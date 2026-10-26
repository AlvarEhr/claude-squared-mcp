import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const PANE = {
  plugin: 'pairs-pane', component: 'Pane', requestId: 'pairs',
  props: { title: 'Pairs', isFocused: false, bodyColumns: 60, placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 }, view: {} },
} as const

function row(name: string, session: string | null = null) {
  return { name, backend: 'codex', model: 'luna', status: 'busy', elapsed_s: 125,
    last_line: 'Running the tests', reason: 'turn open', context: {
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
  on('session.id', () => ({ value: 'manager-session' }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  return clock
}

test('this session by default; toggle all; metadata on desktop and terminal', async ($, on) => {
  base(on)
  on('process.run', (_, e) => {
    expect(e.argv.slice(-2)).toEqual(['--home', '/custom/claude'])
    return { value: { exitCode: 0, stdout: JSON.stringify({ rows: [
      row('scout', 'manager-session'), row('reviewer', 'another-session'), row('builder'),
    ] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  for (const surface of ['desktop', 'terminal'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /scout/ }))?.text).toBe('scout · codex/luna')
    expect(await ui.find({ type: 'Text', text: /reviewer/ })).toBeUndefined()
    expect((await ui.find({ type: 'Text', text: /running for/ }))?.text).toBe('busy · running for 2m 5s')
    expect((await ui.find({ type: 'Text', text: /Watching for/ }))?.text).toContain('this session')
    expect((await ui.find({ type: 'Text', text: /Context/ }))?.text).toContain('20%')
    await ui.press({ key: 'scope' })
    expect((await ui.find({ type: 'Text', text: /reviewer/ }))?.text).toContain('reviewer')
    expect((await ui.find({ type: 'Text', text: /session another-session/ }))?.text).toContain('another-session')
    expect((await ui.find({ type: 'Text', text: /No auto-watch/ }))?.text).toBe('No auto-watch')
    await ui.press({ key: 'scope' })
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
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await clock.advance(2000)
  expect(await ui.find({ type: 'Text', text: /scout/ })).toBeUndefined()
  expect((await ui.find({ type: 'Text', text: /No auto-watch waits/ }))?.text).toContain('this session')
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
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await clock.advance(2000)
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
  mock.clock(on)
  mock.env(on, { HOME: '/home/user' })
  let opened = 0
  on('ui.open', () => { opened++; return { value: { isPlaced: true } } })
  on('session.id', () => ({ value: 'manager-session' }))
  on('process.run', (_, e) => {
    expect(e.argv.slice(-2)).toEqual(['--home', '/home/user/.claude'])
    return { value: { exitCode: 0, stdout: JSON.stringify({ rows: [], error: 'Start the pair MCP server.' }),
      stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.command.run({ command: 'pairs', args: '' })
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
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await $.session.end({ reason: 'clear', sessionId: 'manager-session' })
  expect(await ui.find({ type: 'Text', text: /scout/ })).toBeUndefined()
  await clock.advance(6000)
  expect(calls).toBe(1)
  await ui.unmount()
})

test('context bar and alert are visible in each row, including unknown idle context', async ($, on) => {
  base(on)
  const alerted = { ...row('scout', 'manager-session'), context: {
    used: 110000, window: 200000, percent: 55, alert_at: 110000, above_alert: true } }
  const idle = { ...row('idle-codex'), status: 'idle', context: {
    used: null, window: 200000, percent: null, alert_at: 140000, above_alert: false } }
  on('process.run', () => ({ value: { exitCode: 0, stdout: JSON.stringify({ rows: [alerted, idle] }),
    stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  await $.session.start({ cwd: '/project', surface: 'desktop', isInteractive: false })
  for (const surface of ['desktop', 'terminal'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    const context = (await ui.find({ type: 'Text', text: /^Context/ }))?.text
    expect(context).toContain('[█████░░░░░]')
    expect(context).toContain('110,000 / 200,000')
    expect(context).toContain('ALERT ≥ 110,000')
    await ui.press({ key: 'scope' })
    expect((await ui.find({ type: 'Text', text: /idle-codex/ }))?.text).toContain('idle-codex')
    expect((await ui.find({ type: 'Text', text: /Context.*unknown/ }))?.text)
      .toBe('Context [??????????] ? / 200,000 · unknown')
    await ui.press({ key: 'scope' })
    await ui.unmount()
  }
})
