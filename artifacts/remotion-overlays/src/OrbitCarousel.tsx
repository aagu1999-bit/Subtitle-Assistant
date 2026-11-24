import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from "remotion";

/**
 * Frame-driven orbit: each item moves into the next one's place and the
 * front one rests 0.3s. Same motion as motion.py's orbit, so we can compare
 * Remotion to the Python engine on one request.
 *
 * React Bits components are not imported here on purpose. Their CSS/GSAP
 * clocks freeze under Remotion seek. Port a look onto useCurrentFrame first.
 */
const ITEMS = [
  { id: "sq", phase: 0, bg: "#FFD60A", shape: "square" as const },
  { id: "tri", phase: 120, bg: "#00E5FF", shape: "triangle" as const },
  { id: "ci", phase: 240, bg: "#FF3B6B", shape: "circle" as const },
];

function steppedAngle(t: number, move = 1, hold = 0.3, by = 120) {
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

function Item({
  shape, bg, scale, opacity,
}: { shape: "square" | "triangle" | "circle"; bg: string; scale: number; opacity: number }) {
  const size = 140 * scale;
  const common: React.CSSProperties = {
    width: size, height: size, background: bg, opacity,
    boxShadow: `0 ${12 * scale}px ${28 * scale}px rgba(0,0,0,.35)`,
  };
  if (shape === "circle") return <div style={{ ...common, borderRadius: "50%" }} />;
  if (shape === "square") return <div style={{ ...common, borderRadius: 18 }} />;
  return (
    <div style={{
      width: 0, height: 0, opacity,
      borderLeft: `${size / 2}px solid transparent`,
      borderRight: `${size / 2}px solid transparent`,
      borderBottom: `${size}px solid ${bg}`,
      filter: `drop-shadow(0 ${12 * scale}px ${20 * scale}px rgba(0,0,0,.35))`,
    }} />
  );
}

export const OrbitCarousel: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const t = frame / fps;
  const cx = width / 2, cy = height * 0.52;
  const rx = width * 0.32, ry = height * 0.18;

  const placed = ITEMS.map((it) => {
    const a = ((it.phase + steppedAngle(t)) * Math.PI) / 180;
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
          <Item shape={it.shape} bg={it.bg} scale={it.scale} opacity={it.opacity} />
        </div>
      ))}
    </AbsoluteFill>
  );
};
