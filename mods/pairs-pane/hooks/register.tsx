import { atom, read, update } from 'claude-code'
import type { EngineInterface, PluginOptions, Register } from 'claude-code'
import type { PairRow, PairsView } from '../types'

const PANE = 'pairs'
const view = atom({ plugin: 'pairs-pane', key: 'view' } as const,
  { rows: [], error: null, session: '', home: '' } as PairsView)
const all = atom({ plugin: 'pairs-pane', key: 'all' } as const, false)

function duration(seconds: number | null): string {
  if (seconds === null) return 'unknown'
  const n = Math.max(0, Math.floor(seconds))
  if (n < 60) return `${n}s`
  if (n < 3600) return `${Math.floor(n / 60)}m ${n % 60}s`
  return `${Math.floor(n / 3600)}h ${Math.floor(n % 3600 / 60)}m`
}

function contextLabel(row: PairRow): string {
  const context = row.context
  const used = context?.used == null ? '?' : context.used.toLocaleString()
  const window = context?.window == null ? '?' : context.window.toLocaleString()
  if (context?.percent == null) return `Context [??????????] ${used} / ${window} · unknown`
  const filled = Math.max(0, Math.min(10, Math.floor(context.percent / 10)))
  const bar = '█'.repeat(filled) + '░'.repeat(10 - filled)
  const alert = context.above_alert ? ` · ALERT ≥ ${context.alert_at?.toLocaleString()}` : ''
  return `Context [${bar}] ${used} / ${window} · ${Math.round(context.percent)}%${alert}`
}

let options: PluginOptions = {}
let refreshing = false
let cancel: (() => void) | undefined
let active = true
let generation = 0

function stopPolling() {
  active = false
  generation++
  cancel?.()
  cancel = undefined
}

async function refresh($: EngineInterface) {
  if (refreshing || !active) return
  refreshing = true
  const currentGeneration = generation
  let home = ''
  let session = ''
  try {
    session = await $.session.id()
    const configured = String(options.claudeHome || '') || await $.env.get('CLAUDE_HOME')
    const user = await $.env.get('USERPROFILE') || await $.env.get('HOME')
    home = configured || (user ? `${user}/.claude` : '')
    if (!home) throw new Error('Cannot locate Claude home. Set pairs-pane.claudeHome in /config.')
    const result = await $.process.run([
      String(options.python || 'python'), '-B', `${$.plugin.root}/read_pairs.py`, '--home', home,
    ], { timeoutMs: 5000 })
    if (result.exitCode !== 0 || result.isStdoutTruncated) {
      throw new Error('The pair reader failed. Check pairs-pane.python in /config.')
    }
    const data = JSON.parse(result.stdout)
    if (!Array.isArray(data.rows)) throw new Error('The pair reader returned an invalid snapshot.')
    if (active && generation === currentGeneration) await update($, view, () => ({ rows: data.rows as PairRow[], error: data.error || null, session, home }))
  } catch (error) {
    // Do not leave old waits looking live after a reader/permission failure.
    if (active && generation === currentGeneration) await update($, view, () => ({ rows: [], error: String(error), session, home }))
  } finally {
    refreshing = false
  }
}

function start($: EngineInterface) {
  active = true
  if (!cancel) {
    const timer = $.clock.every(2000, () => refresh($))
    cancel = () => timer.cancel()
  }
}

async function open($: EngineInterface) {
  active = true
  const placed = await $.ui.open({ id: PANE, title: 'Pairs' })
  if (placed.isPlaced) {
    start($)
    await refresh($)
  }
  return placed.isPlaced
}

export const register: Register = (on, configured) => {
  options = configured
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'pairs', description: 'Show pair work and completion watchers' })
    if (options.openOnStart !== false) await open($)
    return next(e)
  })
  on('command.run', { command: 'pairs' }, async $ => {
    const opened = await open($)
    return { text: opened ? 'Pairs pane opened.' : 'No pane surface is available. Run /pairs when the chat UI is attached.' }
  })
  on('ui.close', { id: PANE }, async (_, e, next) => {
    const result = await next(e)
    stopPolling()
    return result
  })
  on('session.end', async ($, e, next) => {
    stopPolling()
    await update($, view, () => ({ rows: [], error: null, session: '', home: '' }))
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const current = await read($, view)
    const showAll = await read($, all)
    const rows = current.rows.filter(row => showAll || row.watches.some(w => w.session_id === current.session))
    return (
      <Box flexDirection="column">
        <Box>
          <Button key="scope" label={showAll ? 'All pairs' : 'This session'}
            onPress={() => { void update($, all, value => !value) }} />
          <Button key="refresh" label="Refresh" onPress={() => { void refresh($) }} />
          <Button key="close" label="Close" onPress={() => { stopPolling(); void $.ui.close({ id: PANE }) }} />
        </Box>
        <Text dimColor>{showAll ? 'All pairs · last reported context' : 'Pairs this session is waiting on'}</Text>
        {current.error && <Text>{current.error}</Text>}
        {!current.error && rows.length === 0 && <Text dimColor>{showAll ? 'No pairs found.' : 'No auto-watch waits for this session.'}</Text>}
        {rows.map(row => (
          <Box flexDirection="column" marginTop={1}>
            <Text bold>{row.name} · {row.backend}/{row.model}</Text>
            <Text bold color={row.context?.above_alert ? 'red' : undefined}>{contextLabel(row)}</Text>
            <Text>{row.status}{row.elapsed_s !== null && row.status !== 'idle' ? ` · ${row.status === 'busy' ? 'running for' : 'work started'} ${duration(row.elapsed_s)}${row.status === 'busy' ? '' : ' ago'}` : ''}</Text>
            {row.last_line && <Text>{row.last_line}</Text>}
            {row.reason && <Text dimColor>{row.reason}</Text>}
            {row.watches.length === 0 && <Text dimColor>No auto-watch</Text>}
            {row.watches.map(w => (
              <Text dimColor>Watching for {duration(w.elapsed_s)} · {w.session_id === current.session ? 'this session' : `session ${w.session_id}`}{w.cwd ? ` · ${w.cwd}` : ''}{w.verified ? '' : ' · PID unverified'}</Text>
            ))}
          </Box>
        ))}
      </Box>
    )
  })
}
