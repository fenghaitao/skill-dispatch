"""Herdr protocol, readiness and delivery tests; no real workers are launched."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'orchestrator'))
import skill_dispatch as sd


@pytest.fixture
def herdr_env(monkeypatch):
    monkeypatch.setenv('HERDR_ENV', '1')
    monkeypatch.setenv('HERDR_WORKSPACE_ID', 'w7')


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def launch(monkeypatch, folder, handler=None, kind='claude', processes=None):
    """Mock Herdr agent calls. `calls` omits process-info launch checks, which
    report the started agent's argv (or `processes`, when given)."""
    calls = []
    clock = Clock()
    monkeypatch.setattr(sd.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(sd.time, 'sleep', clock.sleep)
    agent = {'agent': kind, 'pane_id': 'w7:p9', 'agent_status': 'idle',
             'interactive_ready': True}
    launched = []

    def herdr(*args, **kwargs):
        if args[:2] == ('pane', 'process-info'):
            assert args[2:] == ('--pane', 'w7:p9')
            argv = [kind, *launched[-1][launched[-1].index('--') + 1:]]
            return {'process_info': {'foreground_processes': processes if processes is not None
                                     else [{'argv': argv}]}}
        calls.append((args, kwargs))
        if args[:2] == ('tab', 'create'):
            assert args[2:] == ('--workspace', 'w7', '--cwd', str(folder.resolve()),
                                '--label', 'worker', '--no-focus')
            return {'root_pane': {'pane_id': 'w7:p9'}, 'tab': {'tab_id': 'w7:t4'}}
        if args[:2] == ('agent', 'start'):
            agent['name'] = args[2]
            assert re.fullmatch(r'sd-[0-9a-f]{20}', args[2])
            assert args[3:7] == ('--kind', kind, '--pane', 'w7:p9')
            launched.append(args)
        else:
            assert args[2] == agent['name']
        if handler:
            result = handler(args, kwargs, clock, agent)
            if result is not None:
                return result
        return {'type': 'agent_prompted' if args[1] == 'prompt' else 'agent_started',
                'agent': dict(agent)}

    monkeypatch.setattr(sd, '_herdr', herdr)
    return calls, clock


@pytest.mark.parametrize('opt_in', [False, True])
def test_launch_preserves_cwd_focus_permissions_and_literal_prompt(tmp_path, monkeypatch, herdr_env, opt_in):
    calls, _ = launch(monkeypatch, tmp_path)
    prompt = "Task: 'quoted' $(touch unwanted)\nnext line"
    model = '--bad; $(touch unwanted)'
    result = sd.dispatch_herdr(tmp_path, prompt, 'worker', model, 5,
                               accept_edits=opt_in, allow_tools=['Bash(python3 -m pytest -q)'])
    args, kwargs = calls[1]
    assert args[7:] == ('--timeout', '5000', '--', '--permission-mode',
                        'acceptEdits' if opt_in else 'dontAsk',
                        '--allowedTools=Bash(python3 -m pytest -q)', f'--model={model}')
    assert kwargs['timeout'] == 5
    assert calls[2][0][:4] == ('agent', 'prompt', result['agent'], prompt)
    assert calls[2][0][4:] == ('--wait', '--until', 'working', '--until', 'blocked',
                              '--until', 'idle', '--until', 'done', '--timeout', '10000')
    assert result['accepted'] is True
    assert (result['workspace'], result['tab'], result['pane']) == ('w7', 'w7:t4', 'w7:p9')
    assert len(calls) == 3


@pytest.mark.parametrize('env,workspace', [('0', 'w7'), ('1', ''), ('', '')])
def test_requires_inherited_herdr_context(monkeypatch, tmp_path, env, workspace):
    monkeypatch.setenv('HERDR_ENV', env)
    monkeypatch.setenv('HERDR_WORKSPACE_ID', workspace)
    monkeypatch.setattr(sd, '_herdr', lambda *a, **kw: pytest.fail('contacted Herdr outside a pane'))
    with pytest.raises(sd.DispatchError, match='requires a Herdr terminal'):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None)


@pytest.mark.parametrize('seconds', [0, 3, -1, float('nan'), float('inf')])
def test_invalid_readiness_starts_nothing(monkeypatch, tmp_path, herdr_env, seconds):
    monkeypatch.setattr(sd, '_herdr', lambda *a, **kw: pytest.fail('started with no valid budget'))
    with pytest.raises(sd.DispatchError):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None, seconds)


def test_blocked_startup_is_observed_without_answering_or_restarting(tmp_path, monkeypatch, herdr_env, capsys):
    states = iter(['blocked', 'unknown', 'done'])

    def handler(args, kwargs, clock, agent):
        if args[1] == 'start':
            raise sd.HerdrError('trust', 'agent_not_ready')
        if args[1] == 'get':
            return {'agent': {**agent, 'agent_status': next(states, 'done')}}

    calls, _ = launch(monkeypatch, tmp_path, handler)
    result = sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5)
    assert [a[1] for a, _ in calls[:2]] == ['create', 'start']
    assert all(a[1] == 'get' for a, _ in calls[2:-1])
    assert calls[-1][0][1] == 'prompt'
    assert len(calls) > 6
    assert result['accepted'] is True
    assert 'w7:t4' in capsys.readouterr().err


@pytest.mark.parametrize('late_state', ['idle', 'blocked'])
def test_deadline_prevents_late_prompt(tmp_path, monkeypatch, herdr_env, late_state):
    def handler(args, kwargs, clock, agent):
        if args[1] == 'start':
            clock.now = 5.01
            return {'agent': {**agent, 'agent_status': late_state}}

    calls, _ = launch(monkeypatch, tmp_path, handler)
    with pytest.raises(sd.DispatchError, match='w7:p9.*budget'):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5)
    assert len(calls) == 2


@pytest.mark.parametrize('error', [sd.HerdrError('failed', 'timeout'),
                                   subprocess.TimeoutExpired('herdr', 1)])
def test_start_failure_keeps_worker_identifiers_and_never_retries(tmp_path, monkeypatch, herdr_env, error):
    def handler(args, kwargs, clock, agent):
        raise error

    calls, _ = launch(monkeypatch, tmp_path, handler)
    with pytest.raises(sd.DispatchError, match='w7:t4.*w7:p9.*Inspect this worker'):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5)
    assert len(calls) == 2


@pytest.mark.parametrize('phase', ['start', 'prompt'])
@pytest.mark.parametrize('field,value', [('pane_id', 'w7:p1'), ('name', 'someone-else'), ('agent', 'codex')])
def test_wrong_worker_cannot_be_treated_as_success(tmp_path, monkeypatch, herdr_env, phase, field, value):
    def handler(args, kwargs, clock, agent):
        if args[1] == phase:
            return {'type': 'agent_prompted', 'agent': {**agent, field: value}}

    calls, _ = launch(monkeypatch, tmp_path, handler)
    with pytest.raises(sd.DispatchError):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5)
    assert len(calls) == (2 if phase == 'start' else 3)


@pytest.mark.parametrize('response', [{}, {'root_pane': None, 'tab': []},
                                      {'root_pane': {'pane_id': ''}, 'tab': {'tab_id': 'w7:t4'}}])
def test_missing_created_identifiers_fail_closed(tmp_path, monkeypatch, herdr_env, response):
    monkeypatch.setattr(sd, '_herdr', lambda *a, **kw: response)
    with pytest.raises(sd.DispatchError, match='no tab or root pane'):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None)


@pytest.mark.parametrize('response', [{}, {'type': 'ok'}, {'type': 'agent_prompted', 'agent': None}])
def test_missing_prompt_acknowledgement_fails(tmp_path, monkeypatch, herdr_env, response):
    def handler(args, kwargs, clock, agent):
        if args[1] == 'prompt':
            return response

    launch(monkeypatch, tmp_path, handler)
    with pytest.raises(sd.DispatchError, match='did not acknowledge'):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None)


@pytest.mark.parametrize('returncode,payload,stderr,succeeds', [
    (0, {'result': {'type': 'agent_prompted'}}, False, True),
    (1, {'error': {'code': 'agent_not_ready', 'message': 'trust'}}, True, False),
    (0, {'error': {'code': 'denied'}}, False, False),
    (1, {'result': {'type': 'agent_prompted'}}, False, False),
    (0, [], False, False),
    (0, {'result': None}, False, False),
])
def test_cli_envelopes_and_stderr_errors(monkeypatch, returncode, payload, stderr, succeeds):
    def run(argv, **kwargs):
        assert argv == ['herdr', 'agent', 'get', 'worker']
        assert kwargs == {'capture_output': True, 'text': True, 'timeout': 30}
        body = json.dumps(payload)
        return subprocess.CompletedProcess(argv, returncode, '' if stderr else body, body if stderr else '')

    monkeypatch.setattr(sd.subprocess, 'run', run)
    if succeeds:
        assert sd._herdr('agent', 'get', 'worker') == payload['result']
    else:
        with pytest.raises(sd.HerdrError) as exc:
            sd._herdr('agent', 'get', 'worker')
        if stderr:
            assert exc.value.code == 'agent_not_ready'


def test_invalid_json_reports_cli_diagnostic(monkeypatch):
    monkeypatch.setattr(sd, '_run', lambda *a, **kw: subprocess.CompletedProcess(a, 2, '', 'syntax error'))
    with pytest.raises(sd.HerdrError, match='syntax error'):
        sd._herdr('agent', 'start')


def test_new_tab_shell_startup_race_retries_only_explicit_busy_rejection(tmp_path, monkeypatch, herdr_env):
    starts = 0

    def handler(args, kwargs, clock, agent):
        nonlocal starts
        if args[1] == 'start':
            starts += 1
            if starts < 3:
                raise sd.HerdrError('shell still starting', 'agent_pane_busy')

    calls, _ = launch(monkeypatch, tmp_path, handler)
    assert sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5)['accepted'] is True
    assert [a[1] for a, _ in calls] == ['create', 'start', 'start', 'start', 'prompt']
    assert len({a[2] for a, _ in calls if a[1] == 'start'}) == 1


def test_busy_shell_wait_is_bounded(tmp_path, monkeypatch, herdr_env):
    def handler(args, kwargs, clock, agent):
        raise sd.HerdrError('busy', 'agent_pane_busy')

    calls, clock = launch(monkeypatch, tmp_path, handler)
    with pytest.raises(sd.DispatchError, match='budget'):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 3.2)
    assert clock.now <= 0.3
    assert not any(a[1] == 'prompt' for a, _ in calls)


def test_long_readiness_caps_herdr_start_timeout(tmp_path, monkeypatch, herdr_env):
    calls, _ = launch(monkeypatch, tmp_path)
    sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 600)
    assert calls[1][0][7:9] == ('--timeout', '300000')


@pytest.mark.parametrize('readiness', [{'interactive_ready': False},
                                      {'interactive_ready': None}, {'launch_pending': True}])
def test_idle_alone_does_not_prove_interactive_readiness(tmp_path, monkeypatch, herdr_env, readiness):
    def handler(args, kwargs, clock, agent):
        if args[1] == 'start':
            return {'agent': {**agent, **readiness}}

    calls, _ = launch(monkeypatch, tmp_path, handler)
    sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5)
    assert [a[1] for a, _ in calls] == ['create', 'start', 'get', 'prompt']


@pytest.mark.parametrize('code', ['agent_blocked', 'timeout'])
def test_prompt_failure_is_never_automatically_resubmitted(tmp_path, monkeypatch, herdr_env, code):
    def handler(args, kwargs, clock, agent):
        if args[1] == 'prompt':
            raise sd.HerdrError('submission failed', code)

    calls, _ = launch(monkeypatch, tmp_path, handler)
    with pytest.raises(sd.DispatchError, match='Inspect this worker'):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5)
    assert sum(a[1] == 'prompt' for a, _ in calls) == 1


def test_worker_blocked_after_submission_is_reported(tmp_path, monkeypatch, herdr_env):
    def handler(args, kwargs, clock, agent):
        if args[1] == 'prompt':
            return {'type': 'agent_prompted', 'agent': {**agent, 'agent_status': 'blocked'}}

    launch(monkeypatch, tmp_path, handler)
    with pytest.raises(sd.DispatchError, match='blocked after submission'):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5)


@pytest.mark.parametrize('opt_in', [False, True])
def test_copilot_worker_uses_copilot_kind_and_permission_rules(tmp_path, monkeypatch, herdr_env, opt_in):
    calls, _ = launch(monkeypatch, tmp_path, kind='copilot')
    result = sd.dispatch_herdr(tmp_path, 'task', 'worker', 'gpt-x', 5, accept_edits=opt_in,
                               allow_tools=['shell(python3)'], agent='copilot')
    edits = ['--allow-tool=write', '--allow-tool=shell(mkdir)', '--allow-tool=shell(mv)']
    assert list(calls[1][0][9:]) == ['--', '--no-ask-user', *(edits if opt_in else []),
                                     '--allow-tool=shell(python3)', '--model=gpt-x']
    assert calls[2][0][:2] == ('agent', 'prompt')
    assert (result['kind'], result['accepted']) == ('copilot', True)


def test_copilot_rejects_claude_rules_before_creating_a_tab(tmp_path, monkeypatch, herdr_env):
    monkeypatch.setattr(sd, '_herdr', lambda *a, **kw: pytest.fail('created a tab'))
    with pytest.raises(sd.DispatchError, match='Claude tool rule'):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5,
                          allow_tools=['Bash(python3 -m pytest -q)'], agent='copilot')


@pytest.mark.parametrize('kind', ['claude', 'copilot'])
def test_worker_must_be_a_herdr_agent_of_the_requested_kind(tmp_path, monkeypatch, herdr_env, kind):
    other = 'copilot' if kind == 'claude' else 'claude'

    def handler(args, kwargs, clock, agent):
        if args[1] == 'start':
            return {'agent': {**agent, 'agent': other}}

    calls, _ = launch(monkeypatch, tmp_path, handler, kind=kind)
    with pytest.raises(sd.DispatchError, match='different or missing worker'):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5, agent=kind)
    assert not any(a[1] == 'prompt' for a, _ in calls)


@pytest.mark.parametrize('processes', [
    [{'argv': ['copilot', '--allow-all', '--autopilot', '--no-ask-user']}],  # shell alias
    [{'argv': ['bash']}],
    [],
    [{'argv': 'copilot --no-ask-user'}],
])
def test_unexpected_launch_options_prevent_submission(tmp_path, monkeypatch, herdr_env, processes):
    calls, _ = launch(monkeypatch, tmp_path, kind='copilot', processes=processes)
    with pytest.raises(sd.DispatchError, match='requested options.*Inspect this worker'):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5, agent='copilot')
    assert not any(a[1] == 'prompt' for a, _ in calls)


def test_unrelated_foreground_processes_are_ignored(tmp_path, monkeypatch, herdr_env):
    processes = [{'argv': ['/usr/bin/copilot', '--no-ask-user']}, {'argv': ['node', 'mcp-server']}]
    launch(monkeypatch, tmp_path, kind='copilot', processes=processes)
    assert sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5, agent='copilot')['accepted'] is True


@pytest.mark.parametrize('late', [{'agent_status': 'working', 'state_change_seq': 3},
                                  {'agent_status': 'done', 'state_change_seq': 5}])
def test_stalled_prompt_gate_observes_late_activity_without_resubmitting(
        tmp_path, monkeypatch, herdr_env, late):
    states = iter([{'agent_status': 'idle', 'state_change_seq': 2}] * 3 + [late])

    def handler(args, kwargs, clock, agent):
        if args[1] == 'start':
            return {'agent': {**agent, 'state_change_seq': 2}}
        if args[1] == 'prompt':
            raise sd.HerdrError('no activity within 5000 ms', 'agent_prompt_stalled')
        if args[1] == 'get':
            return {'agent': {**agent, **next(states)}}

    calls, _ = launch(monkeypatch, tmp_path, handler)
    result = sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5)
    assert sum(a[1] == 'prompt' for a, _ in calls) == 1
    assert [a[1] for a, _ in calls[3:]] == ['get'] * 4
    assert result['accepted'] is True and result['send']['late_activity'] is True


@pytest.mark.parametrize('late,match', [
    ({'agent_status': 'done', 'state_change_seq': 2}, 'no activity was observed'),  # unchanged
    ({'agent_status': 'idle', 'state_change_seq': 9}, 'no activity was observed'),
    ({'agent_status': 'done'}, 'no activity was observed'),
    ({'agent_status': 'blocked', 'state_change_seq': 3}, 'blocked after submission'),
])
def test_stalled_prompt_gate_fails_without_evidence(tmp_path, monkeypatch, herdr_env, late, match):
    def handler(args, kwargs, clock, agent):
        if args[1] == 'start':
            return {'agent': {**agent, 'state_change_seq': 2}}
        if args[1] == 'prompt':
            raise sd.HerdrError('no activity within 5000 ms', 'agent_prompt_stalled')
        if args[1] == 'get':
            return {'agent': {**agent, **late}}

    calls, clock = launch(monkeypatch, tmp_path, handler)
    with pytest.raises(sd.DispatchError, match=f'{match}.*Inspect this worker'):
        sd.dispatch_herdr(tmp_path, 'task', 'worker', None, 5)
    assert sum(a[1] == 'prompt' for a, _ in calls) == 1
    assert clock.now <= sd.HERDR_LATE_ACTIVITY_SECONDS + 1
