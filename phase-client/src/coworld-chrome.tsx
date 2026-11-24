import { useEffect, useState } from "react";

import type {
  CoworldReplayController,
  CoworldReplayState,
  CoworldReplayTurnMarker,
} from "./coworld-ws-adapter";

interface CoworldStatus {
  connection?: string;
  gameNumber?: number;
  gamesToWin?: number;
  wins?: [number, number];
  scores?: [number, number];
  matchComplete?: boolean;
}

type CoworldWindow = Window & {
  __coworldStatus?: CoworldStatus;
  __coworldReplayState?: CoworldReplayState;
  __coworldReplay?: CoworldReplayController;
};

function useWindowState<T>(event: string, read: () => T): T {
  const [value, setValue] = useState(read);
  useEffect(() => {
    const update = () => setValue(read());
    window.addEventListener(event, update);
    update();
    return () => window.removeEventListener(event, update);
  }, [event]);
  return value;
}

export function CoworldChrome() {
  const host = window as CoworldWindow;
  const status = useWindowState("coworld-status", () => host.__coworldStatus ?? {});
  const replay = useWindowState<CoworldReplayState | null>(
    "coworld-replay-status",
    () => host.__coworldReplayState ?? null,
  );
  const [showLog, setShowLog] = useState(false);
  const [collapsed, setCollapsed] = useState(readDockCollapsed);
  const setDockCollapsed = (next: boolean) => {
    setCollapsed(next);
    if (next) setShowLog(false);
    try {
      window.localStorage.setItem(DOCK_COLLAPSED_KEY, next ? "1" : "0");
    } catch {
      // Storage can be unavailable; the dock still toggles for this page view.
    }
  };

  useEffect(() => {
    if (!replay) return;
    const toggleDock = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() !== "h" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target && (target.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName))) {
        return;
      }
      event.preventDefault();
      setDockCollapsed(!collapsed);
    };
    window.addEventListener("keydown", toggleDock);
    return () => window.removeEventListener("keydown", toggleDock);
  }, [replay !== null, collapsed]);

  useEffect(() => {
    if (!replay) return;
    const selectHudPerspective = (event: MouseEvent) => {
      const element = event.target instanceof Element ? event.target : null;
      const hud = element?.closest<HTMLElement>("[data-player-hud]");
      if (!hud) return;
      const seat = Number(hud.dataset.playerHud);
      const playerSlot = replay.seatPlayerSlots[seat];
      if (playerSlot === 0 || playerSlot === 1) host.__coworldReplay?.setPerspective(playerSlot);
    };
    document.addEventListener("click", selectHudPerspective, true);
    return () => document.removeEventListener("click", selectHudPerspective, true);
  }, [host, replay]);

  return (
    <>
      {(status.gameNumber || status.matchComplete || status.connection === "disconnected") && (
        <aside className="fixed left-2 top-2 z-[120] rounded-md bg-black/75 px-3 py-1 text-xs text-white shadow-lg backdrop-blur">
          {status.matchComplete ? "Match complete" : `Game ${status.gameNumber ?? 1}`}
          {status.wins && ` · ${status.wins[0]}–${status.wins[1]}`}
          {status.connection === "disconnected" && " · disconnected"}
        </aside>
      )}
      {replay && collapsed && (
        <aside
          aria-label="Replay controls"
          className="fixed bottom-2 left-1/2 z-[120] flex max-w-[calc(100vw-1rem)] -translate-x-1/2 items-center gap-1 rounded-full border border-white/10 bg-zinc-950/70 py-1 pl-1 pr-1 text-[11px] text-white/80 opacity-60 shadow-lg backdrop-blur-md transition-opacity focus-within:opacity-100 hover:opacity-100"
        >
          <button
            type="button"
            className="min-w-12 rounded-full bg-white/15 px-2.5 py-1 font-semibold text-white hover:bg-white/25 disabled:opacity-40"
            disabled={!replay.complete || replay.count === 0}
            onClick={() => (replay.playing ? host.__coworldReplay?.pause() : host.__coworldReplay?.play())}
          >
            {playLabel(replay)}
          </button>
          <button
            type="button"
            aria-label="Show replay controls"
            aria-expanded={false}
            title="Show replay controls (H)"
            className="flex min-w-0 items-center gap-2 rounded-full px-2 py-1 hover:bg-white/10"
            onClick={() => setDockCollapsed(false)}
          >
            <span className="truncate">
              {replay.count === 0 ? "Loading replay" : `Turn ${replay.turnNumber} · ${replay.actionLabel}`}
            </span>
            <span className="shrink-0 tabular-nums text-white/50">
              {replay.count === 0 ? "" : `${Math.min(replay.index + 1, replay.count)}/${replay.count}`}
            </span>
            <span aria-hidden="true" className="shrink-0 text-white/50">▴</span>
          </button>
        </aside>
      )}
      {replay && !collapsed && (
        <aside
          aria-label="Replay controls"
          className="fixed bottom-3 left-1/2 z-[120] w-[min(50rem,calc(100vw-1rem))] -translate-x-1/2 rounded-xl border border-white/15 bg-zinc-950/92 px-3 py-3 text-xs text-white shadow-2xl backdrop-blur-md sm:px-4"
        >
          {showLog && <ReplayMoveLog replay={replay} onSeek={(index) => host.__coworldReplay?.seek(index)} />}

          {replay.outcome && (
            <div
              role="status"
              className="mb-2 rounded-lg border border-amber-300/35 bg-amber-300/10 px-3 py-2"
            >
              <strong className="block text-amber-100">{replay.outcome.headline}</strong>
              <span className="text-[11px] text-amber-50/75">{replay.outcome.detail}</span>
            </div>
          )}

          <div className="mb-2 flex min-w-0 flex-wrap items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              <div className="truncate font-semibold">
                {replay.count === 0
                  ? "Loading replay"
                  : `${replay.gameCount > 1 ? `Game ${replay.gameIndex + 1}/${replay.gameCount} · ` : ""}Turn ${replay.turnNumber} · ${replay.actionLabel}`}
              </div>
              <div className="mt-0.5 hidden text-[10px] text-white/55 sm:block">
                Space: play/pause · ←/→: event · Shift+←/→: turn · Page Up/Down: game · H: hide
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <button
                type="button"
                aria-expanded={showLog}
                className="rounded-md bg-white/10 px-2 py-1.5 hover:bg-white/20"
                onClick={() => setShowLog((value) => !value)}
              >
                Moves
              </button>
              <button
                type="button"
                aria-label="Show priority passes"
                aria-pressed={replay.showPriorityPasses}
                title="Include Pass Priority actions in the timeline"
                className={`rounded-md px-2 py-1.5 ${
                  replay.showPriorityPasses ? "bg-cyan-400 text-black" : "bg-white/10 hover:bg-white/20"
                }`}
                onClick={() =>
                  host.__coworldReplay?.setShowPriorityPasses(!replay.showPriorityPasses)
                }
              >
                Passes
              </button>
              <label className="flex items-center text-white/65">
                <span className="sr-only">Playback speed</span>
                <select
                  aria-label="Playback speed"
                  className="rounded-md border border-white/10 bg-white/10 px-1.5 py-1.5 text-white"
                  value={replay.rate}
                  onChange={(event) => host.__coworldReplay?.setRate(Number(event.currentTarget.value))}
                >
                  {[0.5, 1, 2, 4].map((rate) => (
                    <option key={rate} value={rate} className="bg-zinc-900">
                      {rate}×
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                aria-label="Hide replay controls"
                aria-expanded={true}
                title="Hide replay controls (H)"
                className="rounded-md bg-white/10 px-2 py-1.5 hover:bg-white/20"
                onClick={() => setDockCollapsed(true)}
              >
                Hide
              </button>
            </div>
          </div>

          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-1 text-white/55">
              <span className="mr-1 shrink-0 text-[10px] uppercase tracking-wider">View as</span>
              {replay.playerNames.map((name, playerSlot) => (
                <button
                  key={playerSlot}
                  type="button"
                  aria-pressed={replay.selectedPlayerSlot === playerSlot}
                  className={`max-w-36 truncate rounded-md px-2 py-1 font-medium ${
                    replay.selectedPlayerSlot === playerSlot
                      ? "bg-white text-black"
                      : "bg-white/10 text-white hover:bg-white/20"
                  }`}
                  onClick={() => host.__coworldReplay?.setPerspective(playerSlot)}
                >
                  {name}
                </button>
              ))}
            </div>

            <div className="flex shrink-0 items-center gap-1">
              <button
                type="button"
                aria-label="Previous event"
                title="Previous event (Left arrow)"
                className="rounded-md bg-white/10 px-2.5 py-1.5 hover:bg-white/20 disabled:opacity-35"
                disabled={replay.index <= 0}
                onClick={() => host.__coworldReplay?.step(-1)}
              >
                Back
              </button>
              <button
                type="button"
                className="min-w-16 rounded-md bg-white px-3 py-1.5 font-semibold text-black hover:bg-white/85 disabled:opacity-40"
                disabled={!replay.complete || replay.count === 0}
                onClick={() =>
                  replay.playing ? host.__coworldReplay?.pause() : host.__coworldReplay?.play()
                }
              >
                {playLabel(replay)}
              </button>
              <button
                type="button"
                aria-label="Next event"
                title="Next event (Right arrow)"
                className="rounded-md bg-white/10 px-2.5 py-1.5 hover:bg-white/20 disabled:opacity-35"
                disabled={replay.index >= replay.count - 1}
                onClick={() => host.__coworldReplay?.step(1)}
              >
                Next
              </button>
            </div>
          </div>

          <TurnMarkerRail replay={replay} onSeek={(index) => host.__coworldReplay?.seek(index)} />
          <ReplaySlider
            label="Event"
            value={replay.index}
            count={replay.count}
            display={`${Math.min(replay.index + 1, replay.count)}/${replay.count}`}
            onChange={(value) => host.__coworldReplay?.seek(value)}
          />
          <ReplaySlider
            label="Turn"
            value={replay.turnIndex}
            count={replay.turnCount}
            display={`${replay.turnIndex + 1}/${Math.max(1, replay.turnCount)}`}
            onChange={(value) => host.__coworldReplay?.seekTurn(value)}
          />
          {replay.gameCount > 1 && (
            <ReplaySlider
              label="Game"
              value={replay.gameIndex}
              count={replay.gameCount}
              display={`${replay.gameIndex + 1}/${replay.gameCount}`}
              onChange={(value) => host.__coworldReplay?.seekGame(value)}
            />
          )}
        </aside>
      )}
      {replay && (
        <style>{`body[data-coworld-role="replay"] [data-player-hud] [data-hud-plate] { cursor: pointer; }`}</style>
      )}
    </>
  );
}

const DOCK_COLLAPSED_KEY = "coworld-replay-dock-collapsed";

function readDockCollapsed(): boolean {
  try {
    return window.localStorage.getItem(DOCK_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function playLabel(replay: CoworldReplayState): string {
  if (!replay.complete) return "Loading…";
  if (replay.playing) return "Pause";
  return replay.index >= replay.count - 1 ? "Replay" : "Play";
}

function ReplayMoveLog({ replay, onSeek }: { replay: CoworldReplayState; onSeek: (index: number) => void }) {
  return (
    <aside
      aria-label="Replay move log"
      className="absolute bottom-full right-0 mb-2 max-h-[min(24rem,55vh)] w-full overflow-y-auto rounded-xl border border-white/15 bg-zinc-950/96 p-2 shadow-2xl backdrop-blur-md"
    >
      <div className="sticky top-0 z-10 mb-1 flex items-center justify-between bg-zinc-950/96 px-2 py-1">
        <strong>Move log</strong>
        <span className="text-[10px] text-white/50">{replay.logEntries.length} events</span>
      </div>
      <ol className="space-y-0.5">
        {replay.logEntries.map((entry) => (
          <li key={entry.id}>
            <button
              type="button"
              className={`grid w-full grid-cols-[4rem_1fr_auto] items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-white/10 ${
                entry.eventIndex === replay.index ? "bg-white/15" : ""
              }`}
              onClick={() => onSeek(entry.eventIndex)}
            >
              <span className="text-[10px] tabular-nums text-white/45">
                {replay.gameCount > 1 ? `G${entry.gameNumber} · ` : ""}T{entry.turnNumber}
              </span>
              <span className="truncate">
                {entry.actorName ? `${entry.actorName} · ` : ""}{entry.actionLabel}
              </span>
              <span className="text-[10px] tabular-nums text-white/45">
                {entry.life[0]}–{entry.life[1]}
              </span>
            </button>
          </li>
        ))}
      </ol>
    </aside>
  );
}

function TurnMarkerRail({ replay, onSeek }: { replay: CoworldReplayState; onSeek: (index: number) => void }) {
  return (
    <div className="grid grid-cols-[3.25rem_1fr_3rem] items-end gap-2 text-white/65">
      <span className="text-[10px] uppercase tracking-wider">Turns</span>
      <div className="relative h-5" aria-label="Turn markers">
        <div className="absolute inset-x-0 bottom-1 h-px bg-white/15" />
        {replay.turnMarkers.map((marker) => (
          <TurnMarker
            key={`${marker.gameNumber}-${marker.turnNumber}`}
            marker={marker}
            active={marker.gameNumber === replay.gameNumber && marker.turnNumber === replay.turnNumber}
            playerNames={replay.playerNames}
            onSeek={onSeek}
          />
        ))}
      </div>
      <span />
    </div>
  );
}

function TurnMarker({
  marker,
  active,
  playerNames,
  onSeek,
}: {
  marker: CoworldReplayTurnMarker;
  active: boolean;
  playerNames: [string, string];
  onSeek: (index: number) => void;
}) {
  const position = marker.timelinePosition * 100;
  const title = `${marker.activePlayerName} · Turn ${marker.turnNumber} · ${playerNames[0]} ${marker.life[0]} – ${marker.life[1]} ${playerNames[1]}`;
  const lifeHeight = (life: number) => `${Math.max(8, Math.min(100, (Math.max(0, life) / 20) * 100))}%`;
  return (
    <button
      type="button"
      aria-label={title}
      title={title}
      className={`absolute bottom-0 flex h-5 w-2 -translate-x-1/2 items-end justify-center gap-px rounded-sm ${
        active ? "ring-1 ring-white" : "opacity-75 hover:opacity-100"
      }`}
      style={{ left: `${position}%` }}
      onClick={() => onSeek(marker.eventIndex)}
    >
      <span className="w-0.5 rounded-full bg-cyan-400" style={{ height: lifeHeight(marker.life[0]) }} />
      <span className="w-0.5 rounded-full bg-fuchsia-400" style={{ height: lifeHeight(marker.life[1]) }} />
    </button>
  );
}

function ReplaySlider({
  label,
  value,
  count,
  display,
  onChange,
}: {
  label: string;
  value: number;
  count: number;
  display: string;
  onChange: (value: number) => void;
}) {
  const max = Math.max(0, count - 1);
  return (
    <label className="grid grid-cols-[3.25rem_1fr_3rem] items-center gap-2 py-0.5 text-white/65">
      <span>{label}</span>
      <input
        aria-label={`${label} position`}
        type="range"
        min={0}
        max={max}
        disabled={count <= 1}
        value={Math.min(value, max)}
        onChange={(event) => onChange(Number(event.currentTarget.value))}
        className="h-1.5 w-full cursor-pointer accent-white disabled:cursor-default disabled:opacity-40"
      />
      <output className="text-right tabular-nums text-white/80">{count === 0 ? "–" : display}</output>
    </label>
  );
}
