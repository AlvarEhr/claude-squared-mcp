import { atom, read, update } from 'claude-code'
import type { EngineInterface, PluginOptions, Register } from 'claude-code'
import type { PairRow, PairsView } from '../types'

const PANE = 'pairs'
const view = atom({ plugin: 'pairs-pane', key: 'view' } as const,
  { rows: [], error: null, notice: null, session: '', home: '' } as PairsView)
const all = atom({ plugin: 'pairs-pane', key: 'all' } as const, false)
const expanded = atom({ plugin: 'pairs-pane', key: 'expanded' } as const, [] as string[])
const idle = atom({ plugin: 'pairs-pane', key: 'idle' } as const, false)

function duration(seconds: number | null): string {
  if (seconds === null) return 'unknown'
  const n = Math.max(0, Math.floor(seconds))
  if (n < 60) return `${n}s`
  if (n < 3600) return `${Math.floor(n / 60)}m${n % 60}s`
  return `${Math.floor(n / 3600)}h ${Math.floor(n % 3600 / 60)}m`
}

function contextLabel(row: PairRow, terminal = false): string {
  const context = row.context
  const used = context?.used == null ? '?' : context.used.toLocaleString()
  const window = context?.window == null ? '?' : context.window.toLocaleString()
  if (context?.percent == null) return 'context —'
  const filled = Math.max(0, Math.min(10, Math.floor(context.percent / 10)))
  const bar = '█'.repeat(filled) + '░'.repeat(10 - filled)
  const alert = context.above_alert ? ` · ALERT ≥ ${context.alert_at?.toLocaleString()}` : ''
  return `Context ${terminal ? `[${bar}] ` : ''}${used} / ${window} · ${Math.round(context.percent)}%${alert}`
}

function modelLabel(row: PairRow): string {
  const model = row.model.replace(/^.*\//, '').replace(/\[.*\]$/, '')
  return model.replace(/^(claude|gpt)-/, '').replace(/-\d{8}$/, '')
    .replace(/(\d)-(\d)/g, '$1.$2')
    .split('-').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ')
}

function foreground(row: PairRow): boolean {
  return row.status === 'busy' || row.status === 'needs attention' || row.watches.length > 0
}

function recent(row: PairRow): boolean {
  return row.finished_ago_s != null && row.finished_ago_s <= 7200
}

function orderRows(a: PairRow, b: PairRow): number {
  const group = (row: PairRow) => row.status === 'busy' ? 0 : foreground(row) ? 1 : recent(row) ? 2 : 3
  return group(a) - group(b) || (group(a) === 2 ? (a.finished_ago_s || 0) - (b.finished_ago_s || 0) : 0)
    || a.name.localeCompare(b.name)
}

function statusLabel(row: PairRow): string {
  if (row.status === 'busy') return `working${row.elapsed_s == null ? '' : ` · ${duration(row.elapsed_s)}`}`
  const status = row.status !== 'needs attention' && (row.outcome === 'failed' || row.outcome === 'stopped') ? row.outcome : row.status
  if (!row.finished_at || row.finished_ago_s == null) return status
  const at = new Date(row.finished_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
  const ago = row.finished_ago_s < 60 ? 'just now' : row.finished_ago_s < 3600
    ? `${Math.floor(row.finished_ago_s / 60)} min ago` : `${Math.floor(row.finished_ago_s / 3600)} h ago`
  return `${status} · finished ${at} (${ago})`
}

let options: PluginOptions = {}
let refreshing = false
let cancel: (() => void) | undefined
let active = true
let generation = 0
let readerCache: unknown = {}
let cacheHome = ''

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
    const panes = await $.ui.panes()
    if (!panes.some(pane => pane.id === PANE && pane.isPlaced && pane.isShown)) return
    session = await $.session.id()
    const configured = String(options.claudeHome || '') || await $.env.get('CLAUDE_HOME')
    const user = await $.env.get('USERPROFILE') || await $.env.get('HOME')
    home = configured || (user ? `${user}/.claude` : '')
    if (!home) throw new Error('Cannot locate Claude home. Set pairs-pane.claudeHome in /config.')
    if (home !== cacheHome) { readerCache = {}; cacheHome = home }
    const showAll = await read($, all)
    const previousView = await read($, view)
    const details = (await read($, expanded)).filter(name => previousView.rows.some(row => row.name === name
      && (showAll || row.watches.some(w => w.session_id === session)) && (foreground(row) || recent(row))))
    const result = await $.process.run([
      String(options.python || 'python'), '-B', `${$.plugin.root}/read_pairs.py`, '--home', home,
    ], { timeoutMs: 5000, stdin: JSON.stringify({ session, all: showAll, expanded: details, cache: readerCache }) })
    if (result.exitCode !== 0 || result.isStdoutTruncated) {
      throw new Error('The pair reader failed. Check pairs-pane.python in /config.')
    }
    const data = JSON.parse(result.stdout)
    if (!Array.isArray(data.rows)) throw new Error('The pair reader returned an invalid snapshot.')
    if (active && generation === currentGeneration) {
      readerCache = data.cache || {}
      await update($, view, () => ({ rows: data.rows as PairRow[], error: data.error || null, notice: data.notice || null, session, home }))
    }
  } catch (error) {
    // Do not leave old waits looking live after a reader/permission failure.
    if (active && generation === currentGeneration) await update($, view, () => ({ rows: [], error: String(error), notice: null, session, home }))
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
  } else stopPolling()
  return placed.isPlaced
}

async function changeScope($: EngineInterface, showAll: boolean) {
  await update($, all, () => showAll)
  await refresh($)
}

async function toggleIdle($: EngineInterface) {
  await update($, idle, value => !value)
}

async function toggleDetail($: EngineInterface, name: string) {
  await update($, expanded, value => value.includes(name) ? value.filter(item => item !== name) : [...value, name])
  await refresh($)
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
    readerCache = {}
    await update($, expanded, () => [])
    await update($, idle, () => false)
    await update($, all, () => false)
    await update($, view, () => ({ rows: [], error: null, notice: null, session: '', home: '' }))
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const current = await read($, view)
    const showAll = await read($, all)
    const details = await read($, expanded)
    const showIdle = await read($, idle)
    const rows = current.rows.filter(row => showAll || row.watches.some(w => w.session_id === current.session)).sort(orderRows)
    const dormant = showAll ? rows.filter(row => !foreground(row) && !recent(row)) : []
    const prominent = rows.filter(row => !dormant.includes(row))
    return (
      <Box flexDirection="column">
        <Box gap={1}>
          <Button key="waiting" label={!showAll ? '● Waiting on' : 'Waiting on'}
            onPress={() => { void changeScope($, false) }} />
          <Button key="all" label={showAll ? '● All' : 'All'}
            onPress={() => { void changeScope($, true) }} />
          <Button key="close" label="Close" onPress={() => { stopPolling(); void $.ui.close({ id: PANE }) }} />
        </Box>
        <Text dimColor>{showAll ? 'Working and recently finished' : 'Pairs this session is waiting on'}</Text>
        {current.error && <Text>{current.error}</Text>}
        {current.notice && <Text dimColor>{current.notice}</Text>}
        {!current.error && rows.length === 0 && <Text dimColor>{showAll ? 'No pairs found.' : 'No recorded waits for this session. Choose All to see running pairs. Enable auto-watch on a current server to track waits here.'}</Text>}
        {prominent.map(row => (
          <Box key={`row-${row.name}`} flexDirection="column" marginTop={2} gap={1}>
            <Box justifyContent="space-between" flexWrap="wrap" gap={1}>
              <Box gap={1}><Text bold>{row.name}</Text><Text dimColor>{modelLabel(row)}</Text></Box>
              <Text color={row.status !== 'busy' && row.outcome === 'failed' ? 'red' : undefined}>{statusLabel(row)}</Text>
            </Box>
            <Text bold={row.context?.percent != null} color={row.context?.above_alert ? 'red' : undefined}>{contextLabel(row, e.surface === 'terminal')}</Text>
            {e.surface !== 'terminal' && row.context?.percent != null && (
              <Box key={`bar-${row.name}`} width="100%" height={1} backgroundColor="gray">
                <Box width={`${Math.max(0, Math.min(100, row.context.percent))}%`} height={1}
                  backgroundColor={row.context.above_alert ? 'red' : 'green'} />
              </Box>
            )}
            <Button key={`detail-${row.name}`} label={details.includes(row.name) ? 'Hide detail' : 'Show detail'}
              onPress={() => { void toggleDetail($, row.name) }} />
            {details.includes(row.name) && row.last_line && <Text>{row.last_line}</Text>}
            {details.includes(row.name) && row.reason && <Text dimColor>{row.reason}</Text>}
            {row.watches.map(w => (
              <Text dimColor>watched by {w.session_id === current.session ? 'this session' : `session ${w.session_id.slice(0, 8)}`} · {duration(w.elapsed_s)}{w.verified ? '' : ' · PID unverified'}</Text>
            ))}
          </Box>
        ))}
        {dormant.length > 0 && <Box flexDirection="column" marginTop={2} gap={1}>
          <Button key="idle" label={`${showIdle ? '▾' : '▸'} Idle (${dormant.length})`}
            onPress={() => { void toggleIdle($) }} />
          {showIdle && dormant.map(row => <Box key={`idle-${row.name}`} gap={1}>
            <Text bold wrap="truncate-end">{row.name}</Text><Text dimColor wrap="truncate-end">{modelLabel(row)} · {row.outcome === 'failed' || row.outcome === 'stopped' ? row.outcome : row.status}</Text>
            <Text wrap="truncate-end" color={row.context?.above_alert ? 'red' : undefined}>{contextLabel(row, e.surface === 'terminal')}</Text>
            {e.surface !== 'terminal' && row.context?.percent != null && <Box width={12} height={1} backgroundColor="gray">
              <Box width={`${Math.max(0, Math.min(100, row.context.percent))}%`} height={1}
                backgroundColor={row.context.above_alert ? 'red' : 'green'} />
            </Box>}
          </Box>)}
        </Box>}
      </Box>
    )
  })
}
