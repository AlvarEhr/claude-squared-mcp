export type PairWatch = {
  session_id: string; cwd: string | null; hook_pid: number;
  started_at: string; elapsed_s: number; verified: boolean;
}
export type PairRow = {
  name: string; backend: string; model: string; status: string;
  elapsed_s: number | null; last_line: string; reason: string;
  context: { used: number; window: number; percent: number } | null;
  watches: PairWatch[];
}
export type PairsView = { rows: PairRow[]; error: string | null; session: string; home: string }

declare module 'claude-code' {
  interface PluginState {
    'pairs-pane': { view: PairsView; all: boolean }
  }
}
