import { TOKENS } from "./tokens";
import React from "react";
import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

export type OrbitItem =
  | { id: string; phase: number; kind: "shape"; shape: "square" | "triangle" | "circle"; bg: string }
  | {
      id: string;
      phase: number;
      kind: "image";
      src: string;
      // "cover" (the default) fills the square and crops. A designed mark --
      // a logo, a wordmark -- has to be "contain" or it loses its own edges,
      // and then it needs a plate, because a transparent PNG on a dark frame
      // is an invisible item taking up a slot.
      fit?: "cover" | "contain";
      plate?: string;
    }
  | {
      id: string;
      phase: number;
      kind: "video";
      src: string;
      fit?: "cover" | "contain";
      plate?: string;
    };

export type OrbitProps = {
  duration: number;
  hold: number;
  move: number;
  items: OrbitItem[];
};

export const DEFAULT_ORBIT_PROPS: OrbitProps = {
  duration: 5,
  hold: 0.3,
  move: 1,
  items: [
    { id: "sq", phase: 0, kind: "shape", shape: "square", bg: TOKENS.brandColor },
    { id: "tri", phase: 120, kind: "shape", shape: "triangle", bg: TOKENS.accentColor },
    { id: "ci", phase: 240, kind: "shape", shape: "circle", bg: "#FF3B6B" },
  ],
};

// `by` is one slot, so each step lands every item exactly where the next
// one was. It was hardcoded to 120 degrees, which is one slot only at three
// items -- at six it is two slots (still landing on occupied positions, just
// skipping one), and at two, four or five the ring drifted to positions
// nothing had ever occupied, losing the deliberate snap. Counts other than
// three only became askable recently, which is why this went unnoticed.
// 360/3 is still 120, so every ring rendered before this is unchanged.
function steppedAngle(t: number, move: number, hold: number, by: number) {
  const cycle = move + hold;
  const n = Math.floor(t / cycle);
  const into = t - n * cycle;
  const u = into >= move ? 1 : interpolate(into / move, [0, 1], [0, 1], {
    easing: (x) => x * x * (3 - 2 * x),
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return n * by + u * by;
}

function srcOf(src: string) {
  if (/^(https?:|data:|file:|\/)/.test(src)) return src;
  return staticFile(src);
}

function Item({ item, scale, opacity }: { item: OrbitItem; scale: number; opacity: number }) {
  const size = 140 * scale;
  if (item.kind === "image" || item.kind === "video") {
    const fit = item.fit ?? "cover";
    const shadow = `0 ${12 * scale}px ${28 * scale}px rgba(0,0,0,.35)`;
    const box: React.CSSProperties = {
      width: fit === "contain" ? size * 0.78 : size,
      height: fit === "contain" ? size * 0.78 : size,
      objectFit: fit,
      borderRadius: fit === "contain" ? 0 : 18,
      opacity,
      boxShadow: fit === "contain" ? undefined : shadow,
    };
    // OffthreadVideo, not a <video> tag or @remotion/media: it pulls frames
    // with ffmpeg OUTSIDE the browser. The render already runs at
    // concurrency 1 on software GL under a Replit memory cap, and the one
    // other headless-Chrome job in this repo carries a comment about Replit
    // killing the tab over /dev/shm. Keeping the decode out of the tab is
    // the difference between this working there and not.
    //
    // `muted` is belt and braces -- the file written into public/ has no
    // audio track at all -- but an unmuted item would put an audio stream
    // into what the timeline consumes as a silent alpha overlay.
    const img = item.kind === "video" ? (
      <OffthreadVideo src={srcOf(item.src)} muted style={box} />
    ) : (
      <Img src={srcOf(item.src)} style={box} />
    );
    if (!item.plate) return img;
    return (
      <div
        style={{
          width: size, height: size, borderRadius: 18, opacity,
          background: item.plate, boxShadow: shadow,
          display: "flex", alignItems: "center", justifyContent: "center",
        }}
      >
        {img}
      </div>
    );
  }
  const common: React.CSSProperties = {
    width: size, height: size, background: item.bg, opacity,
    boxShadow: `0 ${12 * scale}px ${28 * scale}px rgba(0,0,0,.35)`,
  };
  if (item.shape === "circle") return <div style={{ ...common, borderRadius: "50%" }} />;
  if (item.shape === "square") return <div style={{ ...common, borderRadius: 18 }} />;
  return (
    <div style={{
      width: 0, height: 0, opacity,
      borderLeft: `${size / 2}px solid transparent`,
      borderRight: `${size / 2}px solid transparent`,
      borderBottom: `${size}px solid ${item.bg}`,
      filter: `drop-shadow(0 ${12 * scale}px ${20 * scale}px rgba(0,0,0,.35))`,
    }} />
  );
}

export const OrbitCarousel: React.FC<OrbitProps> = (props) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const hold = props.hold ?? DEFAULT_ORBIT_PROPS.hold;
  const move = props.move ?? DEFAULT_ORBIT_PROPS.move;
  const items = (props.items && props.items.length ? props.items : DEFAULT_ORBIT_PROPS.items);
  const t = frame / fps;
  const step = steppedAngle(t, move, hold, 360 / Math.max(1, items.length));
  const cx = width / 2, cy = height * 0.52;
  const rx = width * 0.32, ry = height * 0.18;

  const placed = items.map((it) => {
    const a = ((it.phase + step) * Math.PI) / 180;
    const front = (Math.cos(a) + 1) / 2;
    const depth = 0.45;
    return {
      ...it,
      x: cx + rx * Math.sin(a),
      y: cy + ry * Math.cos(a),
      scale: 1 - depth * (1 - front),
      opacity: 1 - depth * 0.5 * (1 - front),
      z: front,
    };
  }).sort((a, b) => a.z - b.z);

  return (
    <AbsoluteFill style={{ background: "transparent" }}>
      {placed.map((it) => (
        <div
          key={it.id}
          style={{
            position: "absolute",
            left: it.x,
            top: it.y,
            transform: "translate(-50%, -50%)",
            zIndex: Math.round(it.z * 100),
          }}
        >
          <Item item={it} scale={it.scale} opacity={it.opacity} />
        </div>
      ))}
    </AbsoluteFill>
  );
};
