"""Offline diagnostics: read setup evidence without initializing local state."""
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
from xml.parsers.expat import ExpatError

import health_garmin as h


SCOPE = "仅检查当前进程访问与本地证据；后台执行和 macOS 权限仍需设备验收。"
RESULTS = frozenset(('pending', 'verified', 'verified_manual', 'verified_conflict',
                     'remote_missing', 'repair_uncertain', 'uncertain', 'inflight',
                     'ignored', 'repair', 'replace', 'conflict', 'deferred'))
UNCERTAIN_RESULTS = frozenset(('inflight', 'uncertain', 'repair_uncertain'))
REVIEW_RESULTS = frozenset(('repair', 'replace', 'conflict', 'verified_conflict', 'remote_missing'))
PHASES = frozenset(('verified', 'deleted_verified', 'closed_partial', 'uncertain',
                    'deleting', 'deleted', 'uploading', 'replacing'))
TERMINAL_PHASES = frozenset(('verified', 'deleted_verified', 'closed_partial'))
STALE_SECONDS = 7200


def _json(path, reject_symlink=True):
    if reject_symlink and path.is_symlink():
        raise ValueError('symlink')
    try:
        return json.loads(h.read_regular_text(path, encoding='utf-8', reject_symlink=reject_symlink))
    except RecursionError as exc:
        raise ValueError('JSON nesting too deep') from exc


class FutureEvidenceError(ValueError):
    pass


def evidence_time(text, reference_time):
    stamp = h.aware(text)
    if (stamp - reference_time).total_seconds() > 600:
        raise FutureEvidenceError('future evidence time')
    return stamp


def attempt_summary(value, *, allowed_stages=('config', 'sync'),
                    allowed_outcomes=('running', 'completed', 'preview', 'failed', 'interrupted'),
                    allow_http=True, reference_time=None):
    if reference_time is None:
        reference_time = h.now()
    fields = {'outcome', 'started_at', 'finished_at', 'stage', 'error_type'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('invalid attempt fields')
    outcome, stage = value['outcome'], value['stage']
    if outcome not in allowed_outcomes or stage not in allowed_stages:
        raise ValueError('invalid attempt values')
    if not isinstance(value['started_at'], str):
        raise ValueError('invalid attempt start')
    started = evidence_time(value['started_at'], reference_time)
    finished = value['finished_at']
    error = value['error_type']
    if outcome == 'running':
        if finished is not None or error is not None:
            raise ValueError('unfinished attempt has terminal fields')
    else:
        if not isinstance(finished, str):
            raise ValueError('invalid attempt finish')
        finished = evidence_time(finished, reference_time)
        if finished < started:
            raise ValueError('attempt ended before it started')
        if outcome in ('completed', 'preview'):
            if stage != allowed_stages[-1] or error is not None:
                raise ValueError('invalid completed attempt')
        elif (not isinstance(error, str) or not re.fullmatch(
                r'[A-Za-z_][A-Za-z0-9_]*' +
                (r'(?: HTTP (?:400|401|403|404|429|500|502|503|504))?' if allow_http else ''), error)):
            raise ValueError('invalid attempt error category')
    # Never forward exception categories or arbitrary fields to user-facing output.
    return {'outcome': outcome, 'stage': stage, 'started_at': started.isoformat(),
            'finished_at': finished.isoformat() if finished is not None else None}


def collect():
    checks = []
    reference_time = h.now()

    def add(identifier, status, message, hint=None, data=None):
        item = {'id': identifier, 'status': status, 'message': message}
        if hint:
            item['hint'] = hint
        if data is not None:
            item['data'] = data
        checks.append(item)

    if sys.version_info >= (3, 12):
        add('python', 'ok', 'Python 版本符合要求。')
    else:
        add('python', 'error', '需要 Python 3.12+。', '运行 bash setup.sh。')
    interpreter = h.BASE / '.venv/bin/python'
    if interpreter.is_file() and os.access(interpreter, os.X_OK):
        add('interpreter', 'ok', '项目虚拟环境解释器可执行。')
    else:
        add('interpreter', 'error', '项目虚拟环境解释器缺失或不可执行。', '运行 bash setup.sh。')
    entry = h.BASE / 'agent_runner.py'
    if entry.is_file() and os.access(entry, os.R_OK):
        add('agent_entry', 'ok', '后台入口存在且当前进程可读取。')
    else:
        add('agent_entry', 'error', '后台入口缺失或当前进程无法读取。', '检查项目文件是否完整。')
    try:
        installed = version('garminconnect')
    except PackageNotFoundError:
        add('dependency', 'error', '未安装 garminconnect。', '运行 bash setup.sh。')
    except (OSError, ValueError):
        add('dependency', 'error', '无法读取 garminconnect 版本信息。', '运行 bash setup.sh。')
    else:
        if installed == h.REQUIRED_GARMIN_VERSION:
            add('dependency', 'ok', 'garminconnect 版本符合固定依赖。')
        else:
            add('dependency', 'error', 'garminconnect 版本与固定依赖不符。', '运行 bash setup.sh。')

    cfg = None
    try:
        cfg = h.validate_config(_json(h.CONFIG, reject_symlink=False))
    except FileNotFoundError:
        add('config', 'error', '尚未初始化配置。', '运行 ./hg init。')
    except h.SafeError as exc:
        add('config', 'error', str(exc), '检查 config.json。')
    except (OSError, UnicodeError, ValueError):
        add('config', 'error', '配置无法读取或不是完整 JSON。', '检查 config.json。')
    else:
        add('config', 'ok', '配置校验通过。')
        add('enabled', 'info' if cfg['enabled'] else 'warn',
            '自动写入已启用。' if cfg['enabled'] else '自动写入已关闭。')

    if cfg is not None:
        try:
            inbox = Path(cfg['inbox']).expanduser()
            files = [path for path in inbox.iterdir() if path.suffix == '.json']
            if not files:
                add('inbox', 'warn', '当前进程可读取 inbox，但没有 JSON 导出。', '在 iPhone 手动导出一次。')
            else:
                samples, records, waiting, _ = h.load_measurements(cfg)
                data = {'files': len(files), 'samples': len(samples),
                        'measurements': len(records), 'waiting_pairs': len(waiting)}
                if waiting:
                    add('inbox', 'error', '导出存在配对歧义。', '运行 ./hg preview 核对配对。', data)
                elif not records:
                    add('inbox', 'warn', '导出有效，但当前范围与来源没有可用测量。',
                        '运行 ./hg preview 核对来源和起始日期。', data)
                else:
                    add('inbox', 'ok', '当前进程可读取导出，解析和配对通过。', data=data)
        except (OSError, ValueError, RuntimeError, h.SafeError):
            add('inbox', 'error', '当前进程无法读取或校验 inbox 导出。',
                '检查目录与下载状态，运行 ./hg preview 查看具体原因。')

    state_available = False
    try:
        if h.STATE.is_symlink():
            add('state', 'error', 'state 不能是符号链接。')
        elif not h.STATE.exists():
            add('state', 'warn', 'state 尚不存在；本次不会创建。')
        elif not h.STATE.is_dir():
            add('state', 'error', 'state 不是目录。')
        elif not os.access(h.STATE, os.R_OK | os.W_OK | os.X_OK):
            add('state', 'error', '当前进程无法读写 state。', '检查本地目录权限。')
        else:
            children = list(h.STATE.iterdir())
            state_available = True
            invalid_file = None
            runtime_names = ('run.lock', 'agent-log.lock', 'sync.log', 'last_attempt.json.tmp',
                             'last_agent_attempt.json.tmp', 'last_run.json.tmp')
            sync_backups = ('sync.log.1', 'sync.log.2', 'sync.log.3')
            log_names = ['agent.out.log', 'agent.err.log']
            readwrite_logs = {base + suffix for base in log_names for suffix in ('', '.1', '.2', '.3')}
            log_names.extend(child.name for child in children
                             if re.fullmatch(r'agent\.(?:out|err)\.log\.[0-9]+', child.name))
            for name in (*runtime_names, *log_names, *sync_backups):
                path = h.STATE / name
                try:
                    mode = path.lstat().st_mode
                    if name in sync_backups:
                        if stat.S_ISDIR(mode):
                            invalid_file = name
                            break
                        continue
                    access = os.W_OK if name in runtime_names else 0
                    if name in readwrite_logs:
                        access = os.R_OK | os.W_OK
                    if not stat.S_ISREG(mode) or (access and not os.access(path, access)):
                        invalid_file = name
                        break
                except FileNotFoundError:
                    continue
                except OSError:
                    invalid_file = name
                    break
            if invalid_file:
                add('state', 'error', '本地运行文件无效或当前进程缺少所需权限。',
                    f'检查 state/{invalid_file} 的文件类型和权限。')
            else:
                add('state', 'info', '当前进程可访问 state。')
    except OSError:
        add('state', 'error', '当前进程无法访问 state。', '检查本地目录权限。')

    if state_available:
        try:
            operations = h.STATE / 'operations'
            pending, total, failed_checks = 0, 0, 0
            if operations.is_symlink():
                raise ValueError('symlink')
            if operations.exists():
                for path in operations.iterdir():
                    if path.suffix != '.json':
                        continue
                    job = _json(path)
                    if (not isinstance(job, dict) or job.get('phase') not in PHASES
                            or not isinstance(job.get('key'), str) or not job['key']):
                        raise ValueError('invalid journal')
                    ids = job.get('record_ids', [])
                    if not isinstance(ids, list) or any(not isinstance(key, str) for key in ids):
                        raise ValueError('invalid journal')
                    if 'last_checked_at' in job or 'last_check_passed' in job:
                        if (not isinstance(job.get('last_checked_at'), str)
                                or type(job.get('last_check_passed')) is not bool):
                            raise ValueError('invalid journal verification')
                        evidence_time(job['last_checked_at'], reference_time)
                        failed_checks += not job['last_check_passed']
                    pending += job['phase'] not in TERMINAL_PHASES
                    total += 1
            add('operations', 'error' if failed_checks else 'warn' if pending else 'ok',
                f'未完成操作 {pending} 项；最近一次核对未通过 {failed_checks} 项。',
                '运行 ./hg verify-operation 核对对应操作记录。' if pending or failed_checks else None,
                {'total': total, 'unfinished': pending, 'failed_checks': failed_checks})
        except FutureEvidenceError:
            add('operations', 'error', '操作记录最近核对时间晚于当前 Mac 时间超过十分钟。',
                '核对 Mac 时间和 state/operations 中的最近核对证据。')
        except (OSError, UnicodeError, ValueError, TypeError, h.SafeError):
            add('operations', 'error', '操作记录无法读取或结构无效。', '检查 state/operations 中的本地证据。')
        try:
            last = _json(h.STATE / 'last_run.json')
            if (not isinstance(last, dict) or not isinstance(last.get('run_at'), str)
                    or not isinstance(last.get('results'), dict)
                    or any(not isinstance(result, str) or result not in RESULTS for result in last['results'].values())):
                raise ValueError('invalid last run')
            ran_at = evidence_time(last['run_at'], reference_time)
            counts = dict(Counter(last['results'].values()))
            local_counts = last.get('local_status_counts', {})
            if (not isinstance(local_counts, dict)
                    or any(key not in RESULTS or type(count) is not int or count < 0
                           for key, count in local_counts.items())):
                raise ValueError('invalid local status counts')
            statuses = set(counts) | {key for key, count in local_counts.items() if count > 0}
            stale = cfg is not None and cfg['enabled'] and (reference_time - ran_at).total_seconds() > STALE_SECONDS
            if UNCERTAIN_RESULTS.intersection(statuses):
                status = 'error'
                message = '最近运行摘要包含请求结果未确定的记录。'
                hint = '先用 ./hg compare 查询并人工核对；结果未确定时不要重复提交。'
                if 'repair_uncertain' in statuses:
                    hint += ' 有修复操作日志时用 ./hg verify-operation 核对。'
            elif REVIEW_RESULTS.intersection(statuses):
                status = 'warn'
                message = '最近运行摘要包含需要人工处理的差异。'
                hint = '运行 ./hg compare 并人工核对；修复或冲突按对账清单处理。'
                if 'remote_missing' in statuses:
                    hint += ' 已确认缺失的记录按 resolve --absent 流程处理。'
            else:
                status = 'info'
                message = '最近运行摘要可读取；所列结果不代表当前记录已核验。'
                hint = None
            if stale:
                if status == 'info':
                    status = 'warn'
                hint = (hint + ' ' if hint else '') + '最近运行摘要距今超过两小时；请核对任务与设备状态。'
            data = {'run_at': ran_at.isoformat(), 'results': counts}
            if 'local_status_counts' in last:
                data['local_status_counts'] = local_counts
                if status in ('error', 'warn') and any(local_counts.values()):
                    hint = (hint + ' ' if hint else '') + '本地状态为最近同步时的快照；运行 ./hg status 核对当前状态。'
            add('last_run', status, message, hint,
                data=data)
        except FileNotFoundError:
            add('last_run', 'warn', '尚无最近运行摘要。')
        except FutureEvidenceError:
            add('last_run', 'error', '最近运行摘要时间晚于当前 Mac 时间超过十分钟。',
                '核对 Mac 时间和 state/last_run.json 中的时间证据。')
        except (OSError, UnicodeError, ValueError, h.SafeError):
            add('last_run', 'error', '最近运行摘要无法读取或结构无效。', '检查 state/last_run.json。')
        try:
            attempt = attempt_summary(_json(h.STATE / 'last_attempt.json'), reference_time=reference_time)
            outcome = attempt['outcome']
            status = 'error' if outcome in ('failed', 'interrupted') else 'warn' if outcome == 'running' else 'info'
            messages = {'running': '最近运行尚无结束证据；可能仍在运行或曾被中断。',
                        'completed': '最近运行命令已结束；不代表全部记录已验证。',
                        'preview': '最近运行只完成关闭自动写入时的本地预览。',
                        'failed': '最近运行失败；请查看本地日志并先核对 Garmin。',
                        'interrupted': '最近运行已中断；上传结果需核对，不能直接重发。'}
            add('last_attempt', status, messages[outcome], data=attempt)
        except FileNotFoundError:
            add('last_attempt', 'info', '尚无运行尝试摘要；兼容已有安装。')
        except FutureEvidenceError:
            add('last_attempt', 'error', '运行尝试摘要时间晚于当前 Mac 时间超过十分钟。',
                '核对 Mac 时间和 state/last_attempt.json 中的时间证据。')
        except (OSError, UnicodeError, ValueError, h.SafeError):
            add('last_attempt', 'error', '运行尝试摘要无法读取或结构无效。', '检查 state/last_attempt.json。')
        try:
            attempt = attempt_summary(_json(h.STATE / 'last_agent_attempt.json'),
                                      allowed_stages=('setup', 'run'),
                                      allowed_outcomes=('running', 'completed', 'failed', 'interrupted'),
                                      allow_http=False, reference_time=reference_time)
            outcome = attempt['outcome']
            status = 'error' if outcome in ('failed', 'interrupted') else 'warn' if outcome == 'running' else 'info'
            messages = {'running': '最近后台入口尚无结束证据；可能仍在运行或曾被中断。',
                        'completed': '最近后台入口阶段已结束；不代表同步完成或全部记录已验证。',
                        'failed': '最近后台入口失败；请查看本地日志并核对运行摘要。',
                        'interrupted': '最近后台入口已中断；先核对运行摘要与 Garmin，不要直接重发。'}
            latest = h.aware(attempt['finished_at'] or attempt['started_at'])
            stale = cfg is not None and cfg['enabled'] and (reference_time - latest).total_seconds() > STALE_SECONDS
            hint = None
            if stale:
                if status == 'info':
                    status = 'warn'
                hint = '后台入口摘要距今超过两小时；请核对定时任务、设备状态与本地日志。'
            add('last_agent_attempt', status, messages[outcome], hint, data=attempt)
        except FileNotFoundError:
            enabled = cfg is not None and cfg['enabled']
            add('last_agent_attempt', 'warn' if enabled else 'info',
                '尚无后台入口摘要；兼容已有安装，不能据此确认后台执行。')
        except FutureEvidenceError:
            add('last_agent_attempt', 'error', '后台入口摘要时间晚于当前 Mac 时间超过十分钟。',
                '核对 Mac 时间和 state/last_agent_attempt.json 中的时间证据。')
        except (OSError, UnicodeError, ValueError, h.SafeError):
            add('last_agent_attempt', 'error', '后台入口摘要无法读取或结构无效。',
                '检查 state/last_agent_attempt.json。')

    enabled = cfg is not None and cfg['enabled']
    try:
        tokens = h.STATE / 'tokens'
        if tokens.is_dir() and not tokens.is_symlink():
            present = any(path.is_file() for path in tokens.iterdir())
            add('tokens', 'info' if present else 'error' if enabled else 'warn',
                '登录令牌文件存在；未读取或验证登录。' if present else '登录令牌目录为空。',
                None if present else '需要同步时运行 ./hg login。')
        elif tokens.exists() or tokens.is_symlink():
            add('tokens', 'error', '登录令牌路径不是有效本地目录。', '检查 state/tokens。')
        else:
            add('tokens', 'error' if enabled else 'warn', '登录令牌目录尚不存在。',
                '需要同步时运行 ./hg login。')
    except OSError:
        add('tokens', 'error', '无法检查登录令牌文件是否存在。', '检查 state/tokens 的本地权限。')

    if sys.platform != 'darwin':
        add('agent', 'info', '当前系统非 macOS，跳过 LaunchAgent 检查。')
    else:
        plist = Path.home() / 'Library/LaunchAgents' / f'{h.LABEL}.plist'
        try:
            if plist.is_symlink():
                raise ValueError('symlink')
            with h.open_regular_file(plist, 'rb') as stream:
                actual = plistlib.load(stream)
            expected = h.agent_plist()
            if not isinstance(actual, dict):
                raise ValueError('invalid plist')
            mismatches = [key for key, value in expected.items() if actual.get(key) != value]
            if 'Program' in actual and actual['Program'] != expected['ProgramArguments'][0]:
                mismatches.append('Program')
            if mismatches:
                add('agent_plist', 'error', '任务配置不匹配：' + '、'.join(mismatches) + '。',
                    '运行 ./hg install-agent 更新任务。')
            else:
                add('agent_plist', 'ok', 'LaunchAgent 配置与当前项目匹配。')
        except FileNotFoundError:
            add('agent_plist', 'error' if enabled else 'warn', '尚未安装 LaunchAgent。',
                '运行 ./hg install-agent。' if enabled else None)
        except (OSError, ValueError, RecursionError, h.SafeError, plistlib.InvalidFileException, ExpatError):
            add('agent_plist', 'error', 'LaunchAgent 配置无法读取或不是有效 plist。',
                '运行 ./hg install-agent 更新任务。')
        try:
            service = subprocess.run(['/bin/launchctl', 'print', f'gui/{os.getuid()}/{h.LABEL}'],
                                     capture_output=True, timeout=5)
            if service.returncode == 0:
                add('agent_service', 'ok', 'LaunchAgent 已载入；尚未证明后台执行成功。')
            else:
                add('agent_service', 'error' if enabled else 'warn', 'LaunchAgent 未载入或服务查询未成功。',
                    '运行 ./hg install-agent，随后核对 ./hg doctor。' if enabled else None)
        except subprocess.TimeoutExpired:
            add('agent_service', 'error', 'LaunchAgent 服务查询超时。', '稍后重试 ./hg doctor。')
        except OSError:
            add('agent_service', 'error', 'launchctl 不可用或无法执行。', '检查本机 launchctl 和当前用户会话。')

    counts = dict(Counter(check['status'] for check in checks))
    status = 'error' if counts.get('error') else 'warning' if counts.get('warn') else 'ok'
    return {'status': status, 'summary': {key: counts.get(key, 0) for key in ('ok', 'info', 'warn', 'error')},
            'scope': SCOPE, 'checks': checks}


def doctor(json_output=False):
    report = collect()
    if json_output:
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    else:
        labels = {'ok': '通过', 'info': '信息', 'warn': '注意', 'error': '错误'}
        overall = {'ok': '通过', 'warning': '有提醒', 'error': '有错误'}
        print('离线诊断：', overall[report['status']])
        for check in report['checks']:
            text = f"[{labels[check['status']]}] {check['message']}"
            if check.get('data'):
                text += ' ' + json.dumps(check['data'], ensure_ascii=False)
            if check.get('hint'):
                text += ' ' + check['hint']
            print(text)
        print(SCOPE)
    return 2 if report['summary']['error'] else 0
