import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const PANE = {
  plugin: 'pairs-pane', component: 'Pane', requestId: 'pairs',
  props: { title: 'Pairs', isFocused: false, bodyColumns: 60, placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 }, view: {} },
} as const
const VISIBLE = { id: 'pairs', title: 'Pairs', isPlaced: true, isShown: true, isFocused: false }

function row(name: string, session: string | null = null) {
  return { name, backend: 'codex', model: 'luna', status: 'busy', elapsed_s: 125,
    detail: { reply: 'Finished reply', reply_at: null, working_on: 'Run tests', logs: ['Running the tests'], note: null }, context: {
      used: 40000, window: 200000, percent: 20, alert_at: 140000, above_alert: false },
    watches: session ? [{ session_id: session, cwd: '/project', hook_pid: 20, elapsed_s: 65,
      started_at: '2026-10-26T12:00:00Z', verified: true }] : [] }
}

function base(on: On) {
  mock.env(on, { CLAUDE_HOME: '/custom/claude', HOME: '/home/user' })
  const clock = mock.clock(on)
  on('command.register', () => ({ value: { command: 'pairs' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.close', () => ({ value: undefined }))
  on('ui.panes', () => ({ value: [VISIBLE] }))
  on('session.id', () => ({ value: 'manager-session' }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  return clock
}

test('this session by default; toggle all; metadata on desktop and terminal', async ($, on) => {
  const clock = base(on)
  on('process.run', (_, e) => {
    expect(e.argv.slice(-2)).toEqual(['--home', '/custom/claude'])
    return { value: { exitCode: 0, stdout: JSON.stringify({ rows: [
      row('scout', 'manager-session'), row('reviewer', 'another-session'), row('builder'),
    ] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  for (const surface of ['desktop', 'terminal'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /scout/ }))?.text).toBe('scout')
    expect((await ui.find({ type: 'Text', text: /^Luna$/ }))?.text).toBe('Luna')
    expect(await ui.find({ type: 'Text', text: /reviewer/ })).toBeUndefined()
    expect((await ui.find({ type: 'Text', text: /working/ }))?.text).toBe('working · 2m5s')
    expect((await ui.find({ type: 'Text', text: /watched by/ }))?.text).toContain('this session')
    expect((await ui.find({ type: 'Text', text: /% used/ }))?.text).toBe('20% used')
    await ui.press({ key: 'all' })
    await clock.advance(1)
    expect((await ui.find({ type: 'Text', text: /reviewer/ }))?.text).toContain('reviewer')
    expect((await ui.find({ type: 'Text', text: /session another-/ }))?.text).toContain('another-')
    expect(await ui.find({ type: 'Text', text: /No auto-watch/ })).toBeUndefined()
    await ui.press({ key: 'waiting' })
    await ui.unmount()
  }
})

test('refresh removes finished waits and close cancels polling', async ($, on) => {
  const clock = base(on)
  let calls = 0
  on('process.run', () => {
    calls++
    return { value: { exitCode: 0, stdout: JSON.stringify({ rows: calls === 1 ? [row('scout', 'manager-session')] : [] }),
      stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await clock.advance(5000)
  expect(await ui.find({ type: 'Text', text: /scout/ })).toBeUndefined()
  expect((await ui.find({ type: 'Text', text: /No recorded waits/ }))?.text).toContain('this session')
  await ui.press({ key: 'close' })
  const stopped = calls
  await clock.advance(6000)
  expect(calls).toBe(stopped)
  await ui.unmount()
})

test('reader failure clears old data and shows an actionable error', async ($, on) => {
  const clock = base(on)
  let calls = 0
  on('process.run', () => ({ value: { exitCode: ++calls === 1 ? 0 : 1,
    stdout: JSON.stringify({ rows: [row('scout', 'manager-session')] }), stderr: 'missing Python',
    isStdoutTruncated: false, isStderrTruncated: false } }))
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await clock.advance(5000)
  expect(await ui.find({ type: 'Text', text: /scout/ })).toBeUndefined()
  expect((await ui.find({ type: 'Text', text: /Check pairs-pane.python/ }))?.text).toContain('/config')
  await ui.unmount()
})

test('another pane still renders through the engine', async ($, on) => {
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  const ui = await $.ui.mount({ ...PANE, requestId: 'other', surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: /engine/ }))?.text).toBe('engine')
  await ui.unmount()
})

test('/pairs opens on demand and resolves the default home', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/home/user' })
  let opened = 0
  on('ui.open', () => { opened++; return { value: { isPlaced: true } } })
  on('ui.panes', () => ({ value: [VISIBLE] }))
  on('session.id', () => ({ value: 'manager-session' }))
  on('process.run', (_, e) => {
    expect(e.argv.slice(-2)).toEqual(['--home', '/home/user/.claude'])
    return { value: { exitCode: 0, stdout: JSON.stringify({ rows: [], error: 'Start the pair MCP server.' }),
      stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.command.run({ command: 'pairs', args: '' })
  await clock.advance(1)
  expect(opened).toBe(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: /Start the pair MCP server/ }))?.text).toContain('MCP')
  await ui.unmount()
})

test('no drawing surface does not launch or poll a reader', async ($, on) => {
  const clock = mock.clock(on)
  on('ui.open', () => ({ value: { isPlaced: false, reason: 'no-surface' } }))
  let calls = 0
  on('process.run', () => { calls++; throw new Error('must not run') })
  const answer = await $.command.run({ command: 'pairs', args: '' })
  expect(answer.text).toContain('No pane surface')
  await clock.advance(6000)
  expect(calls).toBe(0)
})

test('clearing the session clears the view and ends polling', async ($, on) => {
  const clock = base(on)
  let calls = 0
  on('session.end', (_, e) => ({ sessionId: e.sessionId }))
  on('process.run', () => {
    calls++
    return { value: { exitCode: 0, stdout: JSON.stringify({ rows: [row('scout', 'manager-session')] }),
      stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await $.session.end({ reason: 'clear', sessionId: 'manager-session' })
  expect(await ui.find({ type: 'Text', text: /scout/ })).toBeUndefined()
  await clock.advance(6000)
  expect(calls).toBe(1)
  await ui.unmount()
})

test('context bar and alert are visible in each row, including unknown idle context', async ($, on) => {
  const clock = base(on)
  const alerted = { ...row('scout', 'manager-session'), context: {
    used: 110000, window: 200000, percent: 55, alert_at: 110000, above_alert: true } }
  const idle = { ...row('idle-codex'), status: 'idle', context: {
    used: null, window: 200000, percent: null, alert_at: 140000, above_alert: false } }
  on('process.run', () => ({ value: { exitCode: 0, stdout: JSON.stringify({ rows: [alerted, idle] }),
    stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  for (const surface of ['desktop', 'terminal'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    if (surface === 'terminal') expect((await ui.find({ type: 'Text', text: /█████░░░░░/ }))?.text).toBe('█████░░░░░')
    else {
      const tree = JSON.stringify(await ui.drawn())
      expect(tree).toContain('"width":"55%"')
      expect(tree).toContain('"backgroundColor":"red"')
    }
    expect((await ui.find({ type: 'Text', text: /110k/ }))?.text).toBe('110k / 200k')
    expect(await ui.find({ type: 'Text', text: /Alert reached|until alert/ })).toBeUndefined()
    await ui.press({ key: 'all' })
    await clock.advance(1)
    expect(await ui.find({ type: 'Text', text: /idle-codex/ })).toBeUndefined()
    await ui.press({ key: 'idle' })
    expect((await ui.find({ type: 'Text', text: /idle-codex/ }))?.text).toContain('idle-codex')
    expect((await ui.find({ type: 'Text', text: /context —/ }))?.text).toBe('context —')
    await ui.press({ key: 'idle' })
    await ui.press({ key: 'waiting' })
    await ui.unmount()
  }
})

test('scope and expansion reach the reader; collapsed rows show no log detail', async ($, on) => {
  const clock = base(on)
  const requests: { all: boolean; session: string; expanded: string[]; cache: unknown }[] = []
  on('process.run', (_, e) => {
    const request = JSON.parse(e.init?.stdin || '{}')
    requests.push(request)
    return { value: { exitCode: 0, stdout: JSON.stringify({ rows: [row('scout', 'manager-session')],
      cache: { tasks: { example: { summary: { status: 'running' } } } } }),
      stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(requests[0]?.all).toBe(false)
  expect(requests[0]?.session).toBe('manager-session')
  expect(requests[0]?.expanded).toEqual([])
  expect(await ui.find({ type: 'Text', text: /Running the tests/ })).toBeUndefined()
  await ui.press({ key: 'detail-scout' })
  await clock.advance(1)
  expect(requests.at(-1)?.expanded).toEqual(['scout'])
  expect(requests.at(-1)?.cache).toEqual({ tasks: { example: { summary: { status: 'running' } } } })
  expect((await ui.find({ type: 'Text', text: /Running the tests/ }))?.text).toBe('Running the tests')
  await ui.press({ key: 'all' })
  await clock.advance(1)
  expect(requests.at(-1)?.all).toBe(true)
  await ui.unmount()
})

test('hidden or unplaced panes do not start a filesystem refresh', async ($, on) => {
  mock.env(on, { HOME: '/home/user' })
  const clock = mock.clock(on)
  on('command.register', () => ({ value: { command: 'pairs' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'manager-session' }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  let shown = true
  let placed = true
  on('ui.panes', () => ({ value: [{ ...VISIBLE, isShown: shown, isPlaced: placed }] }))
  let calls = 0
  on('process.run', () => {
    calls++
    return { value: { exitCode: 0, stdout: '{"rows":[]}', stderr: '',
      isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  expect(calls).toBe(1)
  shown = false
  await clock.advance(4000)
  expect(calls).toBe(1)
  shown = true
  placed = false
  await clock.advance(4000)
  expect(calls).toBe(1)
  placed = true
  await clock.advance(5000)
  expect(calls).toBe(2)
})

test('all view orders working then recent completions and collapses old idle pairs', async ($, on) => {
  const clock = base(on)
  const rows = [
    { ...row('ancient'), status: 'idle', backend: 'claude', model: 'claude-sonnet-4-6',
      finished_at: '2026-10-26T08:00:00Z', finished_ago_s: 14400 },
    { ...row('older'), status: 'idle', backend: 'claude', model: 'claude-opus-5-5',
      finished_at: '2026-10-26T11:00:00Z', finished_ago_s: 3600 },
    { ...row('recent'), status: 'idle', outcome: 'failed', model: 'gpt-6-astra',
      finished_at: '2026-10-26T11:48:00Z', finished_ago_s: 720 },
    row('working'),
  ]
  on('process.run', () => ({ value: { exitCode: 0, stdout: JSON.stringify({ rows }), stderr: '',
    isStdoutTruncated: false, isStderrTruncated: false } }))
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect((await ui.find({ type: 'Button', key: 'waiting' }))?.text).toContain('● Waiting on')
  expect((await ui.find({ type: 'Text', text: /No recorded waits/ }))?.text).toContain('Choose All')
  await ui.press({ key: 'all' })
  await clock.advance(1)
  expect((await ui.find({ type: 'Button', key: 'all' }))?.text).toContain('● All')
  expect((await ui.findAll({ type: 'Text', text: /^(working|recent|older|ancient)$/ })).map(item => item.text))
    .toEqual(['working', 'recent', 'older'])
  expect((await ui.find({ type: 'Text', text: /^Opus/ }))?.text).toBe('Opus 5.5')
  expect((await ui.find({ type: 'Text', text: /^6 Astra/ }))?.text).toBe('6 Astra')
  expect((await ui.find({ type: 'Text', text: /^failed/ }))?.text).toContain('failed · ')
  expect((await ui.find({ type: 'Button', key: 'idle' }))?.text).toContain('Idle (1)')
  expect(await ui.find({ type: 'Button', key: 'refresh' })).toBeUndefined()
  await ui.press({ key: 'idle' })
  expect((await ui.find({ type: 'Text', text: /^ancient$/ }))?.text).toBe('ancient')
  expect(await ui.find({ type: 'Button', key: 'detail-ancient' })).toBeUndefined()
  await ui.unmount()
})

test('model labels preserve versions and leave unresolved aliases unversioned', async ($, on) => {
  const clock = base(on)
  const models = ['claude-opus-5-5', 'claude-fable-5-1', 'claude-sonnet-5-5', 'claude-haiku-5-5',
    'gpt-6-astra', 'gpt-6.1-sol', 'sol']
  on('process.run', () => ({ value: { exitCode: 0, stdout: JSON.stringify({ rows: models.map((model, index) => ({
    ...row(`pair-${index}`, 'manager-session'), model, backend: model.startsWith('claude-') ? 'claude' : 'codex',
  })) }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const texts = (await ui.findAll({ type: 'Text' })).map(item => item.text)
  for (const label of ['Opus 5.5', 'Fable 5.1', 'Sonnet 5.5', 'Haiku 5.5', '6 Astra', '6.1 Sol', 'Sol']) {
    expect(texts).toContain(label)
  }
  await ui.unmount()
})

test('All and expanded Idle bars use bounded whole-number widths for edge-case fills', async ($, on) => {
  const clock = base(on)
  const fills = [54.7, 123.4, -4.2, NaN, null]
  const rows = ['busy', 'idle'].flatMap(status => fills.map((percent, index) => ({
    ...row(`${status}-${index}`), status,
    context: { ...row('context').context, percent },
  })))
  // JSON.stringify turns NaN into null. Inject the view at the state boundary
  // so this regression exercises barWidth's non-finite branch as well.
  let stateReads = 0
  on('state.get', { plugin: 'pairs-pane', key: 'view' }, async (_, e, next) => {
    stateReads++
    const result = await next(e)
    return { value: { ...result.value,
      value: { rows, error: null, notice: null, session: 'manager-session', home: '/custom/claude' } } }
  })
  on('process.run', () => ({ value: { exitCode: 0, stdout: '{"rows":[]}', stderr: '',
    isStdoutTruncated: false, isStderrTruncated: false } }))
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'all' })
  await clock.advance(1)
  expect(stateReads).toBeGreaterThan(0)
  for (const idleExpanded of [false, true]) {
    if (idleExpanded) await ui.press({ key: 'idle' })
    const boxes = await ui.findAll({ type: 'Box' })
    expect(boxes.length).toBeGreaterThan(0)
    for (const { props } of boxes) {
      if (props.width !== undefined) {
        expect(typeof props.width === 'number' ? Number.isInteger(props.width)
          : typeof props.width === 'string' && /^\d+%$/.test(props.width)).toBe(true)
      }
      // Audit every numeric layout/spacing prop too, including fixed heights,
      // gap and marginTop, so new sizing expressions cannot hide elsewhere.
      for (const value of Object.values(props)) {
        if (typeof value === 'number') expect(Number.isInteger(value)).toBe(true)
        if (typeof value === 'string' && value.endsWith('%')) {
          expect(/^\d+%$/.test(value)).toBe(true)
          expect(Number(value.slice(0, -1))).toBeGreaterThanOrEqual(0)
          expect(Number(value.slice(0, -1))).toBeLessThanOrEqual(100)
        }
      }
    }
    const fillsDrawn = boxes.filter(box => box.props.backgroundColor === 'blue').map(box => box.props.width)
    // Unknown/NaN have no bar; compact Idle rows use the filling glyph alone.
    expect(fillsDrawn).toEqual(['55%', '100%', '0%'])
    expect(await ui.findAll({ type: 'Text', text: /^context —$/ })).toHaveLength(idleExpanded ? 4 : 2)
  }
  await ui.unmount()
})

test('input redraws cached rows while a slow reader is pending; refreshes never overlap', async ($, on) => {
  const clock = base(on)
  let calls = 0
  let release: () => void = () => {}
  on('process.run', async () => {
    calls++
    if (calls === 2) await new Promise<void>(resolve => { release = resolve })
    return { value: { exitCode: 0, stdout: JSON.stringify({ rows: [row('scout', 'manager-session'), row('other')] }),
      stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  expect(calls).toBe(0)
  await clock.advance(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'all' })
  expect(calls).toBe(1) // the click itself starts no read
  expect((await ui.find({ type: 'Text', text: /^other$/ }))?.text).toBe('other')
  await clock.advance(1) // second read remains suspended until release()
  expect(calls).toBe(2)
  await ui.press({ key: 'detail-scout' })
  expect((await ui.find({ type: 'Text', text: /^Finished reply$/ }))?.text).toBe('Finished reply')
  await ui.press({ key: 'detail-scout' })
  expect(await ui.find({ type: 'Text', text: /^Finished reply$/ })).toBeUndefined()
  await ui.press({ key: 'waiting' })
  expect(await ui.find({ type: 'Text', text: /^other$/ })).toBeUndefined()
  await clock.advance(10000)
  expect(calls).toBe(2)
  await ui.press({ key: 'close' })
  release()
  await clock.advance(5000)
  expect(calls).toBe(2)
  await ui.unmount()
})

test('expanded reply and log controls are local; transcript pages load only on request', async ($, on) => {
  const clock = base(on)
  let calls = 0
  const reply = Array.from({ length: 20 }, (_, i) => `reply line ${i}`).join('\n')
  const activity = Array.from({ length: 22 }, (_, i) => `[21:30:00] log line ${i} ${'complete word '.repeat(40)}`)
  on('process.run', (_, e) => {
    calls++
    const request = JSON.parse(e.init?.stdin || '{}')
    const data = request.transcript ? { transcript: { lines: [{ role: 'assistant', at: '2026-10-26T20:30:00Z', text: 'Transcript text' }], next: null, note: null } }
      : { rows: [{ ...row('scout', 'manager-session'), context: { used: 121000, window: 828000, percent: 14.6, above_alert: false },
        detail: { reply, reply_at: '2026-10-26T20:30:00Z', working_on: 'Check the change', logs: activity, note: null } }] }
    return { value: { exitCode: 0, stdout: JSON.stringify(data), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'detail-scout' })
  expect((await ui.find({ type: 'Text', text: /Previous reply/ }))?.text).toContain('Previous reply')
  expect((await ui.find({ type: 'Text', text: /Working on:/ }))?.text).toBe('Working on: Check the change')
  expect(await ui.find({ type: 'Text', text: /reply line 19/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /log line 0 / })).toBeUndefined()
  const before = calls
  await ui.press({ key: 'reply-more-scout' })
  await ui.press({ key: 'logs-more-scout' })
  expect(calls).toBe(before)
  expect((await ui.find({ type: 'Text', text: /reply line 19/ }))?.text).toBe(reply)
  expect((await ui.find({ type: 'Text', text: /log line 0 / }))?.text).toBe(activity[0])
  expect((await ui.find({ key: 'bar-scout' }))?.props.width).toBe('100%')
  expect((await ui.find({ type: 'Text', text: /121k/ }))?.text).toBe('121k / 828k')
  expect((await ui.find({ type: 'Text', text: /% used/ }))?.text).toBe('15% used')
  expect(await ui.find({ type: 'Text', text: /PID|task-id|turn open/ })).toBeUndefined()
  await ui.press({ key: 'transcript-scout' })
  expect((await ui.find({ type: 'Text', text: /Loading transcript/ }))?.text).toContain('Loading')
  expect(calls).toBe(before)
  await clock.advance(1)
  expect((await ui.find({ type: 'Text', text: /^Transcript text$/ }))?.text).toBe('Transcript text')
  const after = calls
  await clock.advance(10000)
  expect(calls).toBe(after)
  await ui.press({ key: 'back' })
  expect((await ui.find({ type: 'Text', text: /^scout$/ }))?.text).toBe('scout')
  await ui.unmount()
})

test('partial history notices stay hidden unless a visible busy or recent row is affected', async ($, on) => {
  const clock = base(on)
  let affected = false
  on('process.run', () => ({ value: { exitCode: 0, stdout: JSON.stringify({ notice: 'old history omitted',
    rows: [{ ...row('scout', 'manager-session'), partial: affected }] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ type: 'Text', text: /read limits|old history/ })).toBeUndefined()
  affected = true
  await clock.advance(5000)
  expect((await ui.find({ type: 'Text', text: /read limits/ }))?.props.dimColor).toBe(true)
  await ui.unmount()
})

test('context widget uses the pair alert level for bar and header indicator colours', async ($, on) => {
  const clock = base(on)
  const cases = [
    { name: 'normal', used: 479900, alert: 600000, color: 'blue', glyph: '◑' },
    { name: 'approaching', used: 480000, alert: 600000, color: 'yellow', glyph: '◑' },
    { name: 'reached', used: 600000, alert: 600000, color: 'red', glyph: '◑' },
    { name: 'override', used: 81000, alert: 100000, color: 'yellow', glyph: '○' },
    { name: 'desktop-example', used: 512600, alert: 620600, color: 'yellow', glyph: '◑' },
  ]
  on('process.run', () => ({ value: { exitCode: 0, stdout: JSON.stringify({ rows: [
    ...cases.map(item => ({ ...row(item.name, 'manager-session'), context: {
      used: item.used, window: 1000000, percent: item.used / 10000, alert_at: item.alert, above_alert: item.used >= item.alert,
    } })),
    { ...row('idle-alert'), status: 'idle', context: { used: 900000, window: 1000000, percent: 90, alert_at: 600000, above_alert: true } },
    { ...row('unknown', 'manager-session'), context: null },
  ] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  for (const item of cases) {
    const bar = await ui.find({ key: `bar-${item.name}` })
    expect(bar?.props).toMatchObject({ width: '100%', height: 1 })
    expect(JSON.stringify(bar?.children)).toContain(`"backgroundColor":"${item.color}"`)
    const indicator = await ui.find({ key: `indicator-${item.name}` })
    expect(indicator?.text).toBe(item.glyph)
    expect(JSON.stringify(indicator?.children)).toContain(`"color":"${item.color}"`)
  }
  expect((await ui.find({ type: 'Text', text: /^512.6k/ }))?.text).toBe('512.6k / 1M')
  expect((await ui.find({ type: 'Text', text: /^51% used$/ }))?.text).toBe('51% used')
  expect(await ui.find({ type: 'Text', text: /until alert|Alert reached/ })).toBeUndefined()
  expect(await ui.find({ key: 'bar-unknown' })).toBeUndefined()
  expect((await ui.find({ type: 'Text', text: /^context —$/ }))?.props.dimColor).toBe(true)
  await ui.press({ key: 'all' })
  await ui.press({ key: 'idle' })
  expect(await ui.find({ key: 'bar-idle-alert' })).toBeUndefined()
  const compact = await ui.find({ key: 'indicator-idle-alert' })
  expect(compact?.text).toBe('●')
  expect(JSON.stringify(compact?.children)).toContain('"color":"red"')
  await ui.unmount()
})

test('interrupted turns have attention status and no-reply labels with matching local times', async ($, on) => {
  const clock = base(on)
  const outcomes = ['orphaned', 'failed', 'stopped', 'crashed']
  on('process.run', () => ({ value: { exitCode: 0, stdout: JSON.stringify({ rows: outcomes.map(outcome => ({
    ...row(outcome, 'manager-session'), status: outcome, outcome,
    finished_at: '2026-10-29T13:15:00+00:00', finished_ago_s: 60,
    detail: { reply: null, reply_at: '2026-10-29T13:15:00', outcome, working_on: null, logs: [], note: null },
  })) }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  for (const outcome of outcomes) {
    await ui.press({ key: `detail-${outcome}` })
    const status = await ui.find({ type: 'Text', text: new RegExp(`^${outcome} · `) })
    const label = await ui.find({ type: 'Text', text: new RegExp(`^No reply \\(turn ${outcome}\\)`) })
    expect(status?.props.color).toBe('yellow')
    expect(status?.text.split(' · ')[1]).toBe(label?.text.split(' · ')[1])
  }
  expect(await ui.find({ type: 'Text', text: /Latest reply|ORPHANED:|idle · finished/ })).toBeUndefined()
  await ui.unmount()
})

test('expanded detail never retains an old log tail when its refresh is unavailable', async ($, on) => {
  const clock = base(on)
  let calls = 0
  on('process.run', () => {
    calls++
    const detail = calls === 2 ? null : { ...row('scout').detail,
      logs: calls === 1 ? ['[21:53:00] [T-562] older day'] : ['[14:10:00] [T-566] new day', '[14:10:01] [T-567] new day'] }
    return { value: { exitCode: 0, stdout: JSON.stringify({ rows: [{ ...row('scout', 'manager-session'), detail }] }),
      stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  await clock.advance(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'detail-scout' })
  expect((await ui.find({ type: 'Text', text: /T-562/ }))?.text).toContain('older day')
  await clock.advance(1)
  expect(await ui.find({ type: 'Text', text: /T-562/ })).toBeUndefined()
  await clock.advance(5000)
  const logs = await ui.findAll({ type: 'Text', text: /T-56[67]/ })
  expect(logs.map(line => line.text)).toEqual(['[14:10:00] [T-566] new day', '[14:10:01] [T-567] new day'])
  await ui.unmount()
})
