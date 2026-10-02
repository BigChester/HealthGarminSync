"""Explicit full-history replacement with a durable backup and no automatic retry."""
from collections import defaultdict
import contextlib
from datetime import date
import json
from pathlib import Path
import shutil
import sqlite3

import health_garmin as h


def signature(rows):
    # Garmin may recompute weightDelta when another day's measurement changes.
    return sorted(json.dumps({k: v for k, v in row.items() if k != 'weightDelta'},
                             sort_keys=True, allow_nan=False) for row in rows)


def load_targets(cfg, start, end):
    _, records, waiting, _ = h.load_measurements(dict(cfg, since=start.isoformat()))
    records = [r for r in records if start <= r.at.date() <= end]
    if waiting or not records:
        raise h.SafeError('完整历史导出为空或存在配对歧义，未执行替换。')
    stamps = sorted(r.at.timestamp() for r in records)
    if any(b - a <= 2 for a, b in zip(stamps, stamps[1:])):
        raise h.SafeError('Apple 测量时间过近，无法可靠区分 FIT 秒精度，未执行替换。')
    return records


class TargetAPI:
    """Permit close weigh-ins only when neighbours match reviewed source targets."""
    def __init__(self, api, target, targets):
        self.api, self.target, self.targets = api, target, targets

    def __getattr__(self, name):
        return getattr(self.api, name)

    def get_body_composition(self, start, end):
        obj = self.api.get_body_composition(start, end)
        index = h.RemoteIndex(obj)
        accepted = set()
        for other in self.targets:
            if other.key == self.target.key:
                continue
            kind, indices, _ = index.decide(other)
            if kind == 'matched':
                accepted.add(indices[0])
        return {**obj, 'dateWeightList': [row for i, row in enumerate(index.rows) if i not in accepted]}


def close_verified_partial(path, cfg, api):
    """Close only a paused batch with every issued request independently verified.

    The next batch must use a newly generated plan. This never resolves an
    uncertain HTTP request or repeats a deletion/upload.
    """
    job = h.read_operation(path)
    if job.get('type') != 'full_replacement' or job.get('phase') != 'uncertain' or cfg.get('enabled'):
        raise h.SafeError('仅能在自动写入关闭时核对暂停的完整替换。')
    events = job['events']
    sent = {e['id'] for e in events if e['kind'] == 'upload_intent'}
    confirmed = {e['id'] for e in events if e['kind'] == 'upload_verified'}
    deleted = {e['id'] for e in events if e['kind'] == 'delete_intent'}
    removed = {e['id'] for e in events if e['kind'] == 'delete_verified'}
    if sent != confirmed or deleted != removed:
        raise h.SafeError('存在未确定请求，不能关闭批次或开始另一批。')
    start, end = date.fromisoformat(job['start']), date.fromisoformat(job['end'])
    targets = load_targets(cfg, start, end)
    if [r.payload() for r in targets] != job['targets']:
        raise h.SafeError('源数据已变化，不能关闭批次。')
    obj = h.fetch_remote_snapshot(api, start, end)
    index = h.RemoteIndex(obj)
    added = set()
    for r in targets:
        if r.key not in confirmed:
            continue
        kind, indices, _ = index.decide(r)
        if kind != 'matched':
            raise h.SafeError('此前上传无法读回匹配，不能关闭批次。')
        added.add(indices[0])
    before = json.loads(h.read_regular_text(Path(job['backup']) / 'remote-before.json'))
    def canonical_rows(rows):
        normalized = []
        for row in rows:
            pk = h.canonical_remote_id(row.get('samplePk'))
            normalized.append({**row, 'samplePk': pk} if pk is not None else row)
        return normalized
    removed_ids = {h.canonical_remote_id(pk) for pk in removed}
    if None in removed_ids:
        raise h.SafeError('删除记录 ID 无法可靠核对，不能关闭批次。')
    current = canonical_rows(index.rows)
    untouched = [r for r in canonical_rows(before['dateWeightList'])
                 if h.canonical_remote_id(r.get('samplePk')) not in removed_ids]
    remaining_raw = [r for i, r in enumerate(index.rows) if i not in added]
    remaining = [r for i, r in enumerate(current) if i not in added]
    # Older versions backed up dateRange's last-per-day response. A paused batch
    # that has deleted nothing may supplement those rows with earlier same-day
    # details, while keeping the original evidence intact.
    if not removed and signature(untouched) != signature(remaining):
        by_id = {str(r['samplePk']): r for r in remaining}
        prior_by_day = {r['calendarDate']: r for r in untouched}
        prior_ids = {str(r['samplePk']) for r in untouched}
        old_intact = all(str(r['samplePk']) in by_id and signature([r]) == signature([by_id[str(r['samplePk'])]]) for r in untouched)
        hidden = [r for r in remaining if str(r['samplePk']) not in prior_ids]
        older_same_day = all(r['calendarDate'] in prior_by_day and h.remote_time(r) < h.remote_time(prior_by_day[r['calendarDate']]) for r in hidden)
        if old_intact and hidden and older_same_day:
            h.write_json(Path(job['backup']) / 'remote-before-details-reconstructed.json',
                         {'dateWeightList': remaining_raw, 'basis': 'Original summary rows intact; only earlier same-day details supplemented; no deletions issued'})
            untouched = remaining
    if any(h.canonical_remote_id(r.get('samplePk')) in removed_ids for r in current) or signature(untouched) != signature(remaining):
        raise h.SafeError('远端出现意外变化，不能关闭批次。')
    job['phase'] = 'closed_partial'
    job['closed_at'] = h.now().isoformat()
    job['partial_verification'] = {'uploads': len(confirmed), 'deletes': len(removed),
                                   'remote_count': len(index.rows)}
    h.write_json(path, job)
    print('已独立核验并关闭部分批次：', job['partial_verification'], '；不会重复已完成请求。', flush=True)
    return obj


def verify(path, api=None):
    job = h.read_operation(path)
    start, end = date.fromisoformat(job['start']), date.fromisoformat(job['end'])
    targets = [h.Measurement(**{**r, 'at': h.aware(r['at'])}) for r in job['targets']]
    backup = Path(job['backup'])
    try:
        api = api or h.api_client()
        obj = h.fetch_remote_snapshot(api, start, end)
        index = h.RemoteIndex(obj)
        decisions = [index.decide(r) for r in targets]
        scoped = [row for row in index.rows if start.isoformat() <= row['calendarDate'] <= end.isoformat()]
    except Exception:
        h.record_failed_check(path, job)
        raise
    with contextlib.closing(h.database()) as db, db:
        plan = h.comparison_plan(db, targets, obj, start, end)
        owners = {indices[0] for kind, indices, _ in decisions if kind == 'matched'}
        matched = (all(kind == 'matched' for kind, _, _ in decisions)
                   and len(owners) == len(targets) == len(scoped)
                   and not plan['extra_remote'])
        if matched:
            for r in targets:
                h.set_status(db, r, 'verified', 'Full-history replacement independently verified')
            job['phase'] = 'verified'
        job['last_checked_at'] = h.now().isoformat()
        job['last_check_passed'] = matched
        job['verification'] = {'matched': sum(index.decide(r)[0] == 'matched' for r in targets),
                               'target_count': len(targets), 'remote_count': len(scoped),
                               'extra': len(plan['extra_remote'])}
        h.write_json(path, job)
        h.write_json(backup / 'remote-after.json', obj)
    print('本次完整历史只读核验：', '匹配' if matched else '未匹配',
          job['verification'], '；操作历史阶段：', job['phase'], flush=True)
    return matched


def execute(args, cfg, api=None):
    if not args.complete_export:
        raise h.SafeError('完整替换需要 --complete-export，确认 Apple 导出包含全部需要保留的测量。')
    path = Path(args.plan).expanduser()
    if path.is_symlink() or not path.is_file():
        raise h.SafeError('请选择本工具生成的有效对账清单。')
    try:
        path.resolve().relative_to((h.STATE / 'plans').resolve())
    except ValueError:
        raise h.SafeError('清单必须位于本项目 state/plans。')
    saved = json.loads(h.read_regular_text(path))
    for key in ('inbox', 'allowed_sources', 'weight_bare_unit', 'fat_bare_unit', 'pair_window_seconds'):
        if saved['config'].get(key) != cfg.get(key):
            raise h.SafeError('来源目录、单位或配对设置已改变，请重新对账。')
    if h.active_operations():
        raise h.SafeError('存在未完成的操作，请先只读核对，不开始另一批替换。')
    start, end = date.fromisoformat(saved['start']), date.fromisoformat(saved['end'])
    records = load_targets(cfg, start, end)
    payloads = [r.payload() for r in records]
    if payloads != [e['measurement'] for e in saved['entries']]:
        raise h.SafeError('Apple 数据已变化，请重新对账。')
    api = api or h.api_client()
    obj = h.fetch_remote_snapshot(api, start, end)
    if signature(obj['dateWeightList']) != signature(saved['remote_snapshot']['dateWeightList']):
        raise h.SafeError('Garmin 数据已变化，请重新对账。')
    index = h.RemoteIndex(obj)
    keep, original_days = set(), defaultdict(list)
    targets_by_day = defaultdict(list)
    for row in index.rows:
        day = date.fromisoformat(row['calendarDate'])
        if start <= day <= end:
            pk = str(row.get('samplePk', ''))
            if not pk.isdigit() or int(pk) <= 0:
                raise h.SafeError('Garmin 记录缺少有效 ID，未执行替换。')
            original_days[day].append(row)
    with contextlib.closing(h.database()) as db, db:
        for r in records:
            kind, candidates, _ = index.decide(r)
            old = h.state_row(db, r)
            if kind != 'matched' and old and old['status'] in ('uncertain', 'inflight', 'repair_uncertain', 'ignored'):
                raise h.SafeError('目标测量有未确定或忽略状态，需要先人工核对。')
            if kind == 'matched':
                row = index.rows[candidates[0]]
                # Retain only records whose Garmin day agrees with the source day.
                if row['calendarDate'] == r.at.date().isoformat():
                    keep.add((str(row['samplePk']), row['calendarDate']))
            targets_by_day[r.at.date()].append(r)
        deletes = sum((str(row['samplePk']), row['calendarDate']) not in keep
                      for rows in original_days.values() for row in rows)
        uploads = len(records) - len(keep)
        print(f'完整替换：目标 {len(records)} 条；保留匹配 {len(keep)} 条；删除旧记录 {deletes} 条；重建/新增 {uploads} 条。', flush=True)
        if input('确认以完整 Apple 历史为准，输入 REPLACE ALL：').strip() != 'REPLACE ALL':
            print('已取消，未修改 Garmin。')
            return
        if payloads != [r.payload() for r in load_targets(cfg, start, end)]:
            raise h.SafeError('确认期间 Apple 数据已变化，未执行替换。')
        latest = h.fetch_remote_snapshot(api, start, end)
        if signature(latest['dateWeightList']) != signature(index.rows):
            raise h.SafeError('确认期间 Garmin 数据已变化，未执行替换。')
        stamp = h.now().strftime('%Y%m%dT%H%M%S%fZ')
        backup = h.STATE / 'backups' / ('full-' + stamp)
        h.private_dir(backup)
        h.write_json(backup / 'remote-before.json', latest)
        h.write_json(backup / 'plan.json', saved)
        h.write_json(backup / 'config.json', cfg)
        h.write_json(backup / 'targets.json', payloads)
        with contextlib.closing(sqlite3.connect(backup / 'sync.sqlite3')) as target, target:
            db.backup(target)
        (backup / 'sync.sqlite3').chmod(0o600)
        for source in Path(cfg['inbox']).glob('*.json'):
            destination = backup / 'exports' / source.name
            h.private_dir(destination.parent)
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
        backed_up = load_targets(dict(cfg, inbox=str(backup / 'exports')), start, end)
        if payloads != [r.payload() for r in backed_up]:
            raise h.SafeError('Apple 导出备份与已确认的测量不一致，未执行替换；请重新对账。')
        journal_path = h.STATE / 'operations' / ('full-' + stamp + '.json')
        job = {'type': 'full_replacement', 'key': 'full-' + stamp, 'phase': 'replacing',
               'start': start.isoformat(), 'end': end.isoformat(), 'targets': payloads,
               'record_ids': [r.key for r in records], 'backup': str(backup), 'events': []}
        h.write_json(journal_path, job)
        def event(kind, identity):
            job['events'].append({'at': h.now().isoformat(), 'kind': kind, 'id': identity})
            h.write_json(journal_path, job)
        try:
            days = sorted(set(original_days) | set(targets_by_day))
            for n, day in enumerate(days, 1):
                if payloads != [r.payload() for r in load_targets(cfg, start, end)]:
                    raise h.SafeError('替换期间 Apple 数据已变化，暂停。')
                current = h.RemoteIndex(api.get_body_composition(day.isoformat(), day.isoformat())).rows
                if signature(current) != signature(original_days[day]):
                    raise h.SafeError('待处理日期 Garmin 数据已变化，暂停。')
                for row in current:
                    pk = str(row['samplePk'])
                    if (pk, row['calendarDate']) in keep:
                        continue
                    event('delete_intent', pk)
                    api.delete_weigh_in(pk, day.isoformat())
                    after = h.RemoteIndex(api.get_body_composition(day.isoformat(), day.isoformat())).rows
                    if not h.original_id_absent(after, pk, calendar=day.isoformat()):
                        raise h.SafeError('删除后旧记录仍可见，暂停，不重复删除。')
                    event('delete_verified', pk)
                for r in targets_by_day[day]:
                    target_api = TargetAPI(api, r, records)
                    kind = h.classify_remote(r, h.remote_for(target_api, r))
                    if kind == 'matched':
                        h.set_status(db, r, 'verified', 'Matched full-history target; local LBM retained')
                        continue
                    if kind != 'absent':
                        raise h.SafeError('新增前出现未解决的远端冲突，暂停。')
                    event('upload_intent', r.key)
                    h.set_status(db, r, 'pending', 'Explicit complete-history replacement; old records removed')
                    if h.process_record(target_api, db, r, reviewed=True) != 'verified':
                        raise h.SafeError('新增请求尚未读回匹配，暂停，不自动重传。')
                    event('upload_verified', r.key)
                print(f'日期完成 {n}/{len(days)}：{day}', flush=True)
            if not verify(journal_path, api):
                raise h.SafeError('最终完整历史未匹配，保留日志并暂停。')
            print('完整历史替换已完成；备份：', backup, flush=True)
        except Exception as exc:
            job = h.read_operation(journal_path)
            job['phase'] = 'uncertain'
            job['error'] = h.safe_exception(exc)
            if isinstance(exc, h.SafeError):
                job['reason'] = str(exc)
            h.write_json(journal_path, job)
            raise h.SafeError('完整替换已暂停，不重复删除或上传；操作记录：' + str(journal_path) + '；' + h.safe_exception(exc)) from exc
