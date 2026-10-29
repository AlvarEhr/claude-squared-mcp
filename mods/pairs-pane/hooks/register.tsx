import { atom, read, update } from 'claude-code'
import type { EngineInterface, PluginOptions, Register } from 'claude-code'
import type { EntryView, PairRow, PairsView, TranscriptView } from '../types'
import { localTime } from './time'

const PANE = 'pairs'
const view = atom({ plugin: 'pairs-pane', key: 'view' } as const,
  { rows: [], error: null, notice: null, session: '', home: '' } as PairsView)
const all = atom({ plugin: 'pairs-pane', key: 'all' } as const, false)
const expanded = atom({ plugin: 'pairs-pane', key: 'expanded' } as const, [] as string[])
const idle = atom({ plugin: 'pairs-pane', key: 'idle' } as const, false)
const replies = atom({ plugin: 'pairs-pane', key: 'replies' } as const, [] as string[])
const logs = atom({ plugin: 'pairs-pane', key: 'logs' } as const, [] as string[])
const transcript = atom({ plugin: 'pairs-pane', key: 'transcript' } as const, null as TranscriptView | null)
const entry = atom({ plugin: 'pairs-pane', key: 'entry' } as const, null as EntryView | null)

function tokens(value: number | null | undefined): string {
  if (value == null) return '—'
  return value >= 1000000 ? `${Math.round(value / 100000) / 10}M` : value >= 1000 ? `${Math.round(value / 100) / 10}k` : String(value)
}

function duration(seconds: number | null): string {
  if (seconds === null) return 'unknown'
  const n = Math.max(0, Math.floor(seconds))
  if (n < 60) return `${n}s`
  if (n < 3600) return `${Math.floor(n / 60)}m${n % 60}s`
  return `${Math.floor(n / 3600)}h ${Math.floor(n % 3600 / 60)}m`
}

function knownContext(row: PairRow): boolean {
  return row.context?.percent != null && Number.isFinite(row.context.percent)
    && row.context.used != null && row.context.window != null
}

function contextColor(row: PairRow): string {
  if (!knownContext(row)) return 'gray'
  const context = row.context!
  if (context.above_alert || (context.alert_at != null && context.used! >= context.alert_at)) return 'red'
  if (context.alert_at != null && context.used! >= context.alert_at * 0.8) return 'yellow'
  return 'blue'
}

function contextIndicator(row: PairRow): string {
  if (!knownContext(row)) return '○'
  return ['○', '◔', '◑', '◕', '●'][Math.max(0, Math.min(4, Math.round(row.context!.percent! / 25)))]
}

function contextBar(row: PairRow): string {
  const filled = Math.max(0, Math.min(10, Math.floor(row.context!.percent! / 10)))
  return '█'.repeat(filled) + '░'.repeat(10 - filled)
}

function modelLabel(row: PairRow): string {
  const model = row.model.replace(/^.*\//, '').replace(/\[.*\]$/, '')
  return model.replace(/^(claude|gpt)-/, '').replace(/-\d{8}$/, '')
    .replace(/(\d)-(\d)/g, '$1.$2')
    .split('-').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ')
}

function foreground(row: PairRow): boolean {
  return row.status === 'busy' || row.status === 'needs attention' || row.status === 'orphaned'
    || row.status === 'crashed' || row.watches.length > 0
}

function interrupted(outcome: string | null | undefined): boolean {
  return ['orphaned', 'failed', 'stopped', 'crashed'].includes(outcome || '')
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
  if (row.details_skipped) return `${row.status === 'busy' ? 'working · ' : ''}details skipped (budget)`
  if (row.status === 'busy') return `working${row.elapsed_s == null ? '' : ` · ${duration(row.elapsed_s)}`}`
  if (interrupted(row.status) || interrupted(row.outcome)) {
    const at = localTime(row.finished_at)
    return `${interrupted(row.status) ? row.status : row.outcome}${at ? ` · ${at}` : ''}`
  }
  const status = row.status !== 'needs attention' && (row.outcome === 'failed' || row.outcome === 'stopped') ? row.outcome : row.status
  if (!row.finished_at || row.finished_ago_s == null) return status
  const at = localTime(row.finished_at)
  const ago = row.finished_ago_s < 60 ? 'just now' : row.finished_ago_s < 3600
    ? `${Math.floor(row.finished_ago_s / 60)} min ago` : `${Math.floor(row.finished_ago_s / 3600)} h ago`
  return `${status} · finished ${at} (${ago})`
}

const RING: Record<string, string> = { red: '#e5484d', yellow: '#e5a00d', blue: '#3e7bfa', gray: '#8a8a8a' }

// A small filling ring, like the desktop's own context indicator.
function ring(row: PairRow): string {
  const percent = knownContext(row) ? Math.max(0, Math.min(100, row.context!.percent!)) : 0
  const circumference = 2 * Math.PI * 5.5
  return '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14">'
    + '<circle cx="7" cy="7" r="5.5" fill="none" stroke="#8a8a8a" stroke-opacity="0.3" stroke-width="2.5"/>'
    + `<circle cx="7" cy="7" r="5.5" fill="none" stroke="${RING[contextColor(row)]}" stroke-width="2.5" `
    + `stroke-dasharray="${(circumference * percent / 100).toFixed(2)} ${circumference.toFixed(2)}" transform="rotate(-90 7 7)"/></svg>`
}

function statusColor(row: PairRow): string | undefined {
  if (row.status === 'busy') return 'success'
  if (row.status === 'crashed' || row.outcome === 'crashed') return 'error'
  if (row.status === 'needs attention' || interrupted(row.status) || interrupted(row.outcome)) return 'warning'
  return undefined
}

function watchLabel(row: PairRow, session: string): string {
  return row.watches.map(w => `watched by ${w.session_id === session ? 'this session' : `session ${w.session_id.slice(0, 8)}`}`
    + ` · ${duration(w.elapsed_s)}${w.verified ? '' : ' · unverified'}`).join(', ')
}

function replyHeading(row: PairRow): string {
  const d = row.detail!
  const at = localTime(d.reply_at)
  if (interrupted(d.outcome)) return `No reply (turn ${d.outcome})${at ? ` · ${at}` : ''}`
  // A busy pair's last reply belongs to the task before the one now running.
  if (row.status === 'busy') return `Previous task done${at ? ` · ${at}` : ''} · next task running`
  return `Latest reply${at ? ` · ${at}` : ''}`
}

// A brief's opening, short enough for one caption line.
function brief(message: string): string {
  const first = message.split(/(?<=[.!?])\s/)[0]
  return first.length > 140 ? `${first.slice(0, 139)}…` : first
}

// The first lines of a markdown reply, closing a code fence the cut left open.
function clip(lines: string[]): string {
  const head = lines.slice(0, 12)
  const fences = head.filter(line => /^\s*(```|~~~)/.test(line)).length
  return head.join('\n') + (fences % 2 ? '\n```' : '') + (lines.length > 12 ? '\n\n…' : '')
}

type LogEntry = { at: string; kind: string; text: string; color?: string; dim?: boolean; quiet?: boolean; tag?: string }

// Codex item types, named as a reader would say them.
const CODEX_TOOLS: Record<string, string> = {
  command_execution: 'shell', file_change: 'edit', collab_tool_call: 'agent', mcp_tool_call: 'mcp', web_search: 'search',
}

// Log text flattens newlines to " / " and keeps JSON escapes; drop rules and doubled backslashes.
function tidy(text: string): string {
  return text.replace(/\\\\/g, '\\').replace(/(?:\s*\/\s*)?[-=.~_*]{4,}(?:\s*\/)?/g, ' ')
    .replace(/(?:\s\/\s*){2,}/g, ' / ').replace(/\s+/g, ' ').replace(/^\s*\/\s*|\s*\/\s*$/g, '').trim()
}

// main.log lines read "[HH:MM:SS] [T-n] [kind] text"; time, turn and kind are each optional.
export function parseLog(line: string): LogEntry {
  const parsed = parseRaw(line)
  const tag = /^(?:\[\d\d:\d\d:\d\d\]\s*)?\[(T-\d+)\]/.exec(line)?.[1]
  return { ...parsed, text: tidy(parsed.text), ...(tag ? { tag } : {}) }
}

function parseRaw(line: string): LogEntry {
  const match = /^(?:\[(\d\d:\d\d):\d\d\]\s*)?(?:\[T-\d+\]\s*)?(?:\[([a-z_]+(?::[a-z_]+)?)( ERR)?\]\s*)?([\s\S]*)$/.exec(line)
  if (!match) return { at: '', kind: '', text: line }
  const [, at = '', kind = '', failed, body = ''] = match
  if (kind === 'tool_use') {
    const call = /^([\w.-]+)\(([\s\S]*?)\)?$/.exec(body)
    const name = call ? call[1].replace(/^mcp__.+?__/, '') : 'tool'
    return { at, kind: CODEX_TOOLS[name] || name, text: call ? call[2] : body, color: 'suggestion' }
  }
  if (kind === 'tool_result') return failed
    ? { at, kind: '↳ failed', text: body.replace(/^exit \d+\s*/, ''), color: 'error' }
    : { at, kind: '↳', text: body.replace(/^exit 0\s*/, ''), dim: true, quiet: true }
  if (kind === 'error') return { at, kind: 'error', text: body, color: 'error' }
  if (kind === 'thinking' || kind === 'summary') return { at, kind: 'thinking', text: body, dim: true }
  if (kind === 'text') return { at, kind: 'says', text: body, color: 'claude' }
  if (kind.startsWith('system:')) return { at, kind: kind.slice(7).replace(/_/g, ' '), text: body, dim: true }
  return { at, kind: kind.replace(/_/g, ' '), text: body }
}

let options: PluginOptions = {}
let refreshing = false
let cancel: (() => void) | undefined
let active = true
let generation = 0
let readerCache: unknown = {}
let cacheHome = ''
let scopeCache: Record<string, PairsView> = {}
let scheduled: (() => void) | undefined
let pending = false
// Set by open, cleared by close and session end: whether a drawing may restart reads.
let wanted = false

function requestRefresh($: EngineInterface) {
  if (!active) return
  if (refreshing) { pending = true; return }
  if (scheduled) return
  const timer = $.clock.after(1, () => { scheduled = undefined; void refresh($) })
  scheduled = () => timer.cancel()
}

function stopPolling() {
  active = false
  generation++
  cancel?.()
  cancel = undefined
  scheduled?.()
  scheduled = undefined
  pending = false
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
    if (home !== cacheHome) { readerCache = {}; scopeCache = {}; cacheHome = home }
    const showAll = await read($, all)
    const previousView = await read($, view)
    const details = (await read($, expanded)).filter(name => previousView.rows.some(row => row.name === name
      && (showAll || row.watches.some(w => w.session_id === session)) && (foreground(row) || recent(row))))
    const page = await read($, transcript)
    if (page && !page.loading) return
    const request = page ? { transcript: { pair: page.pair, offset: page.offset } }
      : { session, all: showAll, expanded: details, cache: readerCache }
    const result = await $.process.run([
      String(options.python || 'python'), '-B', `${$.plugin.root}/read_pairs.py`, '--home', home,
    ], { timeoutMs: 5000, stdin: JSON.stringify(request) })
    if (result.exitCode !== 0 || result.isStdoutTruncated) {
      throw new Error('The pair reader failed. Check pairs-pane.python in /config.')
    }
    const data = JSON.parse(result.stdout)
    if (page) {
      if (!data.transcript) throw new Error('Transcript unavailable.')
      if (active && generation === currentGeneration) await update($, transcript, current => current?.pair === page.pair
        && current.offset === page.offset ? { ...current, ...data.transcript, loading: false } : current)
      return
    }
    if (!Array.isArray(data.rows)) throw new Error('The pair reader returned an invalid snapshot.')
    if (active && generation === currentGeneration) {
      readerCache = data.cache || {}
      const rows = (data.rows as PairRow[]).map(row => ({ ...row,
        // Keep collapsed detail for instant reopening, but a requested detail
        // that could not be refreshed must never retain an old physical tail.
        detail: row.detail || (!details.includes(row.name) && previousView.home === home
          ? previousView.rows.find(old => old.name === row.name && old.finished_at === row.finished_at
            && old.status === row.status && old.outcome === row.outcome)?.detail : null) }))
      const snapshot = { rows, error: data.error || null, notice: data.notice || null, session, home,
        budget_limited: !!data.budget_limited }
      scopeCache[String(showAll)] = snapshot
      if ((await read($, all)) === showAll && !(await read($, transcript))) await update($, view, () => snapshot)
    }
  } catch (error) {
    // Do not leave old waits looking live after a reader/permission failure.
    if (active && generation === currentGeneration) {
      if (await read($, transcript)) await update($, transcript, page => page ? { ...page, loading: false, note: String(error) } : null)
      else {
        scopeCache = {}
        await update($, view, () => ({ rows: [], error: String(error), notice: null, session, home }))
      }
    }
  } finally {
    refreshing = false
    if (pending && active) { pending = false; requestRefresh($) }
  }
}

function start($: EngineInterface) {
  active = true
  if (!cancel) {
    const timer = $.clock.every(5000, () => { requestRefresh($) })
    cancel = () => timer.cancel()
  }
}

async function open($: EngineInterface) {
  active = true
  wanted = true
  const placed = await $.ui.open({ id: PANE, title: 'Pairs' })
  if (placed.isPlaced) {
    start($)
    requestRefresh($)
  } else stopPolling()
  return placed.isPlaced
}

async function changeScope($: EngineInterface, showAll: boolean) {
  await update($, all, () => showAll)
  if (scopeCache[String(showAll)]) await update($, view, () => scopeCache[String(showAll)])
  requestRefresh($)
}

async function toggleIdle($: EngineInterface) {
  await update($, idle, value => !value)
}

async function toggleDetail($: EngineInterface, name: string) {
  await update($, expanded, value => value.includes(name) ? value.filter(item => item !== name) : [...value, name])
  if ((await read($, expanded)).includes(name)) requestRefresh($)
}

async function toggleMore($: EngineInterface, name: string, kind: 'reply' | 'log') {
  if (kind === 'reply') await update($, replies, value => value.includes(name) ? value.filter(item => item !== name) : [...value, name])
  else await update($, logs, value => value.includes(name) ? value.filter(item => item !== name) : [...value, name])
}

async function openTranscript($: EngineInterface, name: string, offset = 0) {
  await update($, transcript, () => ({ pair: name, offset, next: null, loading: true, lines: [], note: null }))
  requestRefresh($)
}

// One activity entry open at a time; a tool call's full input and output
// are read only when it is opened.
async function toggleEntry($: EngineInterface, pair: string, key: string, tag: string | null) {
  const current = await read($, entry)
  if (current?.pair === pair && current.key === key) { await update($, entry, () => null); return }
  await update($, entry, () => ({ pair, key, tag, loading: !!tag }))
  if (!tag) return
  let detail: Partial<EntryView> = { note: 'Detail unavailable.' }
  try {
    const home = cacheHome || String(options.claudeHome || '') || await $.env.get('CLAUDE_HOME')
      || `${await $.env.get('USERPROFILE') || await $.env.get('HOME')}/.claude`
    const result = await $.process.run([String(options.python || 'python'), '-B', `${$.plugin.root}/read_pairs.py`, '--home', home],
      { timeoutMs: 5000, stdin: JSON.stringify({ tool: { pair, tag } }) })
    if (result.exitCode === 0 && !result.isStdoutTruncated) detail = JSON.parse(result.stdout).tool || detail
  } catch { /* the note above stands */ }
  await update($, entry, value => value?.pair === pair && value.key === key ? { ...value, ...detail, loading: false } : value)
}

async function closeTranscript($: EngineInterface) {
  await update($, transcript, () => null)
  requestRefresh($)
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
    wanted = false
    stopPolling()
    return result
  })
  on('session.end', async ($, e, next) => {
    wanted = false
    stopPolling()
    readerCache = {}
    scopeCache = {}
    await update($, expanded, () => [])
    await update($, idle, () => false)
    await update($, all, () => false)
    await update($, replies, () => [])
    await update($, logs, () => [])
    await update($, transcript, () => null)
    await update($, entry, () => null)
    await update($, view, () => ({ rows: [], error: null, notice: null, session: '', home: '' }))
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const resolved = $.ui.resolve(e) as unknown as Record<string, any>
    const desktop = e.surface !== 'terminal'
    const Svg = desktop ? resolved.Svg : undefined
    const Markdown = desktop ? resolved.Markdown : undefined
    const Code = desktop ? resolved.Code : undefined
    // Type sizes, weights and tabular digits are drawn by the desktop surface
    // alone; older terminal engines refuse the props outright.
    const cap = desktop ? { size: 'caption' as const } : {}
    const heading = desktop ? { size: 'heading' as const } : { bold: true }
    const semi = desktop ? { weight: 'semibold' as const } : { bold: true }
    const num = desktop ? { tabular: true } : {}
    const mono = desktop ? { mono: true, tabular: true } : {}
    const current = await read($, view)
    const showAll = await read($, all)
    const details = await read($, expanded)
    const showIdle = await read($, idle)
    const moreReplies = await read($, replies)
    const moreLogs = await read($, logs)
    const page = await read($, transcript)
    const opened = await read($, entry)
    // A pane opened before any surface attached stopped polling; the first
    // drawing after a surface places it starts the reads again.
    if (wanted && !cancel) { start($); requestRefresh($) }
    if (page) return <Box flexDirection="column" gap={1}>
      <Box gap={1}>
        <Button key="back" label="‹ Pairs" onPress={() => { void closeTranscript($) }} />
        {page.offset > 0 && <Button key="transcript-first" label="Start" dimColor onPress={() => { void openTranscript($, page.pair) }} />}
        {page.next !== null && <Button key="transcript-next" label="Next page" dimColor onPress={() => { void openTranscript($, page.pair, page.next!) }} />}
      </Box>
      <Text {...heading} {...semi}>{page.pair}</Text>
      {page.loading && <Text dimColor>Loading transcript…</Text>}
      {page.note && <Text dimColor>{page.note}</Text>}
      {!page.loading && page.lines.length === 0 && !page.note && <Text dimColor>No messages on this page.</Text>}
      {page.lines.map(line => <Box flexDirection="column" marginTop={1}>
        <Text {...cap} dimColor {...semi}>{line.role} · {localTime(line.at)}</Text><Text wrap="wrap">{line.text}</Text>
      </Box>)}
    </Box>
    const rows = current.rows.filter(row => showAll || row.watches.some(w => w.session_id === current.session)).sort(orderRows)
    const skipped = rows.filter(row => row.details_skipped)
    const dormant = showAll ? rows.filter(row => !row.details_skipped && (row.compact || (!foreground(row) && !recent(row)))) : []
    const prominent = rows.filter(row => !dormant.includes(row) && !row.details_skipped)
    const waitingCount = current.rows.filter(row => row.watches.some(w => w.session_id === current.session)).length

    const indicator = (row: PairRow) => <Box key={`indicator-${row.name}`}>{Svg
      ? <Svg source={ring(row)} alt={knownContext(row) ? `${Math.round(row.context!.percent!)}% of context used` : 'context unknown'} width={14} height={14} />
      : <Text dimColor={!knownContext(row)} color={contextColor(row)}>{contextIndicator(row)}</Text>}</Box>

    const block = (source: string, language?: string) => Code
      ? <Code source={source} language={language} />
      : <Text {...cap} wrap="wrap">{source}</Text>

    // An opened entry: a tool call's full input and output, or a long line in full.
    const entryDetail = (item: LogEntry, shown: EntryView) => <Box flexDirection="column" gap={1} marginTop={1} marginBottom={1} paddingLeft={2}>
      {shown.loading && <Text {...cap} dimColor>Loading…</Text>}
      {!shown.loading && !shown.tag && <Text {...cap} wrap="wrap">{item.text}</Text>}
      {!shown.loading && shown.tag && shown.input == null && <Text {...cap} wrap="wrap">{item.text}</Text>}
      {shown.note && <Text {...cap} dimColor>{shown.note}</Text>}
      {shown.input ? block(shown.input, shown.language) : null}
      {shown.output || shown.status ? <Text {...cap} {...semi} dimColor color={shown.status && /error|failed|exit [1-9]/.test(shown.status) ? 'error' : undefined}>
        Output{shown.status ? ` · ${shown.status}` : ''}</Text> : null}
      {shown.output ? block(shown.output) : null}
    </Box>

    const activity = (row: PairRow) => {
      // Newest first, one line each: new entries land on top instead of
      // pushing the list around as the log grows.
      // Successful tool results only repeat the call above them; failures stay.
      const entries = row.detail!.logs.slice().reverse().map(parseLog).filter(item => !item.quiet && (item.text || item.kind))
      const open = moreLogs.includes(row.name)
      return <Box flexDirection="column">
        {(open ? entries : entries.slice(0, 8)).map((item, index) => {
          const key = `${item.tag || item.at}|${item.kind}|${item.text.slice(0, 60)}`
          const shown = opened?.pair === row.name && opened.key === key ? opened : null
          return <Box key={`log-${row.name}-${index}`} flexDirection="column" minWidth={0}>
            <Box gap={1} minWidth={0}>
              <Text {...cap} {...mono} dimColor>{item.at || '--:--'}</Text>
              <Text {...cap} {...semi} color={item.color} dimColor={item.dim}>{item.kind}</Text>
              <Box flexShrink={1} flexGrow={1} minWidth={0} overflow="hidden">
                <Button key={`entry-${row.name}-${index}`} plain onPress={() => { void toggleEntry($, row.name, key, item.tag || null) }}>
                  <Text {...cap} dimColor={item.dim && !shown} wrap="truncate-end">{item.text}</Text>
                </Button>
              </Box>
            </Box>
            {shown && entryDetail(item, shown)}
          </Box>
        })}
        {entries.length === 0 && <Text {...cap} dimColor>No activity logged yet.</Text>}
        {entries.length > 8 && <Button key={`logs-more-${row.name}`} plain dimColor
          label={open ? 'Show fewer' : `Show ${entries.length - 8} more`} onPress={() => { void toggleMore($, row.name, 'log') }} />}
      </Box>
    }

    const card = (row: PairRow) => {
      const open = details.includes(row.name)
      const d = row.detail
      const replyLines = d?.reply?.split('\n') || []
      const fullReply = moreReplies.includes(row.name)
      return <Box key={`row-${row.name}`} flexDirection="column" marginTop={1} paddingX={1} minWidth={0} overflow="hidden"
        borderStyle="round" borderDimColor={row.status !== 'busy'} borderColor={row.status === 'busy' ? 'claude' : undefined}>
        <Box justifyContent="space-between" gap={1}>
          <Box gap={1} alignItems="center" flexShrink={1} minWidth={0}>
            {indicator(row)}
            <Text {...semi} wrap="truncate-end">{row.name}</Text>
            <Text {...cap} dimColor wrap="truncate-end">{modelLabel(row)}</Text>
          </Box>
          <Text {...cap} {...num} color={statusColor(row)} dimColor={!statusColor(row)}>{statusLabel(row)}</Text>
        </Box>
        <Box justifyContent="space-between" gap={1} flexWrap="wrap">
          {knownContext(row)
            ? <Text {...cap} {...num} dimColor>{Math.round(row.context!.percent!)}% used · {tokens(row.context!.used)} / {tokens(row.context!.window)}</Text>
            : <Text {...cap} dimColor>context —</Text>}
          {row.watches.length > 0 && <Text {...cap} dimColor>{watchLabel(row, current.session)}</Text>}
        </Box>
        {!desktop && knownContext(row) && <Text color={contextColor(row)}>{contextBar(row)}</Text>}
        {row.status === 'busy' && d?.working_on && <Text {...cap} italic dimColor wrap="truncate-end">↳ {brief(d.working_on)}</Text>}
        <Box gap={2}>
          <Button key={`detail-${row.name}`} plain dimColor label={open ? '▾ Hide details' : '▸ Details'}
            onPress={() => { void toggleDetail($, row.name) }} />
          <Button key={`transcript-${row.name}`} plain dimColor label="Transcript" onPress={() => { void openTranscript($, row.name) }} />
        </Box>
        {open && <Box flexDirection="column" gap={1} marginBottom={1}>
          {!d && <Text {...cap} dimColor>Loading reply and activity…</Text>}
          {d && <Box flexDirection="column" gap={1}>
            <Text {...cap} {...semi} dimColor>{replyHeading(row)}</Text>
            {d.reply != null && (Markdown
              ? <Markdown key={`reply-${row.name}`} text={fullReply ? d.reply : clip(replyLines)} />
              : <Text wrap="wrap">{fullReply ? d.reply : replyLines.slice(0, 12).join('\n')}</Text>)}
            {replyLines.length > 12 && <Button key={`reply-more-${row.name}`} plain dimColor
              label={fullReply ? 'Show less' : `Show full reply (${replyLines.length} lines)`} onPress={() => { void toggleMore($, row.name, 'reply') }} />}
            {d.note && <Text {...cap} dimColor>{d.note}</Text>}
            <Text {...cap} {...semi} dimColor>Activity · newest first</Text>
            {activity(row)}
          </Box>}
        </Box>}
      </Box>
    }

    const section = (label: string, list: PairRow[]) => list.length > 0 && <Box key={`section-${label}`} flexDirection="column" marginTop={1}>
      <Text {...cap} {...semi} dimColor>{label.toUpperCase()} · {list.length}</Text>
      {list.map(card)}
    </Box>

    return (
      <Box flexDirection="column">
        <Box justifyContent="space-between" alignItems="center">
          <Box gap={1}>
            <Button key="waiting" variant={showAll ? undefined : 'primary'} dimColor={showAll}
              label={`Waiting on${waitingCount ? ` · ${waitingCount}` : ''}`} onPress={() => { void changeScope($, false) }} />
            <Button key="all" variant={showAll ? 'primary' : undefined} dimColor={!showAll}
              label="All" onPress={() => { void changeScope($, true) }} />
          </Box>
          <Button key="close" plain dimColor label="Close" onPress={() => { wanted = false; stopPolling(); void $.ui.close({ id: PANE }) }} />
        </Box>
        {current.error && <Box marginTop={1}><Text color="error">{current.error}</Text></Box>}
        {!current.error && rows.length === 0 && <Box marginTop={1}><Text dimColor>{showAll ? 'No pairs found.' : 'No recorded waits for this session. Choose All to see running pairs. Enable auto-watch on a current server to track waits here.'}</Text></Box>}
        {section('Working', prominent.filter(row => row.status === 'busy'))}
        {section('Needs attention', prominent.filter(row => row.status !== 'busy' && foreground(row)))}
        {section('Recently finished', prominent.filter(row => row.status !== 'busy' && !foreground(row)))}
        {skipped.map(row => <Box key={`skipped-${row.name}`} gap={1} marginTop={1}>
          <Text {...semi}>{row.name}</Text><Text {...cap} dimColor>{modelLabel(row)} · {statusLabel(row)}</Text>
        </Box>)}
        {dormant.length > 0 && <Box flexDirection="column" marginTop={2}>
          <Button key="idle" plain dimColor label={`${showIdle ? '▾' : '▸'} Idle (${dormant.length})`}
            onPress={() => { void toggleIdle($) }} />
          {showIdle && dormant.map(row => <Box key={`idle-${row.name}`} gap={1} alignItems="center" paddingX={1}>
            {indicator(row)}
            <Text {...semi} wrap="truncate-end">{row.name}</Text>
            <Text {...cap} dimColor wrap="truncate-end">{modelLabel(row)} · {row.outcome === 'failed' || row.outcome === 'stopped' ? row.outcome : row.status}</Text>
            <Text {...cap} {...num} dimColor wrap="truncate-end">
              {knownContext(row) ? `${tokens(row.context!.used)} / ${tokens(row.context!.window)} · ${Math.round(row.context!.percent!)}%` : 'context —'}
            </Text>
          </Box>)}
        </Box>}
        {(current.budget_limited || rows.some(row => row.partial && (foreground(row) || recent(row)))) && <Box marginTop={2}>
          <Text {...cap} dimColor>{current.budget_limited ? current.notice || 'Details skipped (budget).' : 'Some working/recent pair details are unavailable within the read limits.'}</Text>
        </Box>}
      </Box>
    )
  })
}
