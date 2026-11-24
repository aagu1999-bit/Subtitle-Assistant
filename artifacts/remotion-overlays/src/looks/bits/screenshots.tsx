import React from "react";
import { AbsoluteFill, Easing, Img, interpolate, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { GOLD } from "./shared";

export type ShotItem = { id?: string; src: string };

export type ShotTourProps = {
  duration?: number;
  caption?: string;
  items?: ShotItem[];
};

function srcOf(src: string) {
  if (/^(https?:|data:|file:|\/)/.test(src)) return src;
  return staticFile(src);
}

const FakeUI: React.FC<{ variant: number }> = ({ variant }) => {
  const screens = [
    { title: "Home", bars: [78, 52, 64] },
    { title: "Today", bars: [42, 88, 56] },
    { title: "Stats", bars: [70, 36, 90] },
  ];
  const s = screens[Math.abs(variant) % screens.length];
  return (
    <div style={{ padding: 28, height: "100%", background: "#0f0f14", color: "#fff", boxSizing: "border-box" }}>
      <div style={{ fontSize: 14, opacity: 0.45, letterSpacing: 1 }}>9:41</div>
      <div style={{ fontSize: 32, fontWeight: 800, color: GOLD, margin: "22px 0 18px" }}>{s.title}</div>
      {s.bars.map((w, i) => (
        <div key={i} style={{
          height: 16, width: `${w}%`, borderRadius: 8, marginBottom: 14,
          background: i === 1 ? GOLD : "#2a2a33",
        }} />
      ))}
      <div style={{
        marginTop: 28, height: 200, borderRadius: 18,
        background: "linear-gradient(180deg,#1c1c24,#111118)",
        border: "1px solid #2a2a33",
      }} />
    </div>
  );
};

const Phone: React.FC<{ children: React.ReactNode; style?: React.CSSProperties }> = ({ children, style }) => (
  <div style={{
    width: 390, height: 720, borderRadius: 46, background: "#16161a",
    padding: 10, boxSizing: "border-box",
    boxShadow: "0 32px 80px rgba(0,0,0,.5), 0 0 0 2px #2e2e34",
    ...style,
  }}>
    <div style={{
      width: "100%", height: "100%", borderRadius: 36, overflow: "hidden",
      background: "#0b0b10", position: "relative",
    }}>
      <div style={{
        position: "absolute", top: 10, left: "50%", width: 96, height: 22,
        marginLeft: -48, borderRadius: 12, background: "#000", zIndex: 2,
      }} />
      {children}
    </div>
  </div>
);

const ScreenBody: React.FC<{ item?: ShotItem; variant: number }> = ({ item, variant }) => {
  if (item?.src) {
    return <Img src={srcOf(item.src)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />;
  }
  return <FakeUI variant={variant} />;
};

function useTour(count: number) {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const n = Math.max(1, count);
  const slot = durationInFrames / n;
  const idx = Math.min(n - 1, Math.floor(frame / slot));
  return { frame, durationInFrames, n, slot, idx, local: frame - idx * slot };
}

/** 2.5D pop: one screenshot at a time, overshoot, depth shadow. */
export const ScreenshotTourLook: React.FC<ShotTourProps> = ({
  items = [], caption = "screenshot tour",
}) => {
  const shots = items.filter((it) => it?.src);
  const count = shots.length > 0 ? Math.max(shots.length, 2) : 3;
  const { slot, idx, local } = useTour(count);
  const item = shots[idx % Math.max(1, shots.length)] as ShotItem | undefined;
  const scale = interpolate(local, [0, 8, 16], [0.78, 1.08, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });
  const y = interpolate(local, [0, 12], [70, 0], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });
  const op = interpolate(local, [0, 6, slot - 8, slot], [0, 1, 1, 0], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill style={{ background: "transparent", alignItems: "center", justifyContent: "center" }}>
      <div style={{
        fontFamily: "Inter, Anton, sans-serif", fontWeight: 800, fontSize: 42,
        color: GOLD, marginBottom: 28, opacity: op, letterSpacing: -0.8,
      }}>{caption}</div>
      <div style={{ transform: `translateY(${y}px) scale(${scale})`, opacity: op }}>
        <Phone>
          <ScreenBody item={shots.length ? item : undefined} variant={idx} />
        </Phone>
      </div>
    </AbsoluteFill>
  );
};

/** “It looks like this”: a fan of phones, the front one is the current screen. */
export const LooksLikeThisLook: React.FC<ShotTourProps> = ({
  items = [], caption = "looks like this",
}) => {
  const shots = items.filter((it) => it?.src);
  const count = 3;
  const { idx } = useTour(Math.max(count, shots.length || count));
  const slots = [-1, 0, 1];
  return (
    <AbsoluteFill style={{
      background: "transparent", alignItems: "center", justifyContent: "center",
      perspective: 1200, fontFamily: "Inter, Anton, sans-serif",
    }}>
      <div style={{
        fontWeight: 800, fontSize: 48, color: GOLD, marginBottom: 36, letterSpacing: -1,
      }}>{caption}</div>
      <div style={{ position: "relative", width: 900, height: 760 }}>
        {slots.map((off) => {
          const which = (idx + off + count) % count;
          const front = off === 0;
          const item = shots[which % Math.max(1, shots.length)] as ShotItem | undefined;
          return (
            <div key={off} style={{
              position: "absolute", left: 255 + off * 210, top: front ? 0 : 40,
              transform: `rotateY(${off * -22}deg) scale(${front ? 1 : 0.82})`,
              zIndex: front ? 3 : 1,
              opacity: front ? 1 : 0.72,
            }}>
              <Phone>
                <ScreenBody item={shots.length ? item : undefined} variant={which} />
              </Phone>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
