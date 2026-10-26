export type PairWatch = {
  session_id: string; cwd: string | null; hook_pid: number;
  started_at: string; elapsed_s: number; verified: boolean;
}
export type PairRow = {
  name: string; backend: string; model: string; status: string;
  elapsed_s: number | null; last_line: string; reason: string;
  finished_at?: string | null; finished_ago_s?: number | null; outcome?: string | null;
  context: {
    used: number | null; window: number | null; percent: number | null;
    alert_at: number | null; above_alert: boolean;
  } | null;
  watches: PairWatch[];
}
export type PairsView = { rows: PairRow[]; error: string | null; notice: string | null; session: string; home: string }

declare module 'claude-code' {
  interface PluginState {
    'pairs-pane': { view: PairsView; all: boolean; expanded: string[]; idle: boolean }
  }
}
