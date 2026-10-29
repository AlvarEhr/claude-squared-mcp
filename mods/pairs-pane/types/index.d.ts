export type PairWatch = {
  session_id: string; cwd: string | null; hook_pid: number;
  started_at: string; elapsed_s: number; verified: boolean;
}
export type PairRow = {
  name: string; backend: string; model: string; status: string;
  elapsed_s: number | null;
  detail?: { reply: string | null; reply_at: string | null; outcome?: string | null;
    working_on: string | null; logs: string[]; note: string | null } | null;
  partial?: boolean;
  finished_at?: string | null; finished_ago_s?: number | null; outcome?: string | null;
  context: {
    used: number | null; window: number | null; percent: number | null;
    alert_at: number | null; above_alert: boolean;
  } | null;
  watches: PairWatch[];
}
export type PairsView = { rows: PairRow[]; error: string | null; notice: string | null; session: string; home: string }
export type TranscriptView = { pair: string; offset: number; next: number | null; loading: boolean;
  lines: { role: string; at: string; text: string }[]; note: string | null }

declare module 'claude-code' {
  interface PluginState {
    'pairs-pane': { view: PairsView; all: boolean; expanded: string[]; idle: boolean;
      replies: string[]; logs: string[]; transcript: TranscriptView | null }
  }
}
