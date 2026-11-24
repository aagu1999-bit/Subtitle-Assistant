import React from "react";
import { Easing, interpolate } from "remotion";
import { BitsStage, GOLD, MINT, useClock, type BitsCardProps, type BitsCountProps } from "./shared";

const Card: React.FC<{
  width?: number; height?: number; children: React.ReactNode;
  style?: React.CSSProperties; glow?: string;
}> = ({ width = 420, height = 520, children, style, glow = GOLD }) => (
  <div style={{
    width, height, borderRadius: 28, padding: 28, boxSizing: "border-box",
    background: "linear-gradient(180deg, rgba(255,255,255,0.14), rgba(255,255,255,0.04))",
    border: "1px solid rgba(255,255,255,0.22)",
    boxShadow: `0 24px 80px rgba(0,0,0,0.35), 0 0 40px ${glow}22`,
    color: "#fff", fontFamily: "Inter, system-ui, sans-serif",
    display: "flex", flexDirection: "column", ...style,
  }}>{children}</div>
);

export const ProfileCardLook: React.FC<BitsCardProps> = ({ text = "ALEX CHEN" }) => {
  const { frame, fps } = useClock();
  const p = interpolate(frame, [0, fps * 0.5], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.out(Easing.cubic),
  });
  return (
    <BitsStage>
      <div style={{ transform: `translateY(${(1 - p) * 40}px) scale(${0.92 + p * 0.08})`, opacity: p }}>
        <Card>
          <div style={{
            width: 120, height: 120, borderRadius: 60, margin: "12px auto 20px",
            background: `conic-gradient(from ${frame * 4}deg, ${GOLD}, ${MINT}, #00E5FF, ${GOLD})`,
            padding: 4,
          }}>
            <div style={{
              width: "100%", height: "100%", borderRadius: 56, background: "#111",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 44, fontWeight: 800, color: GOLD,
            }}>{text.slice(0, 1)}</div>
          </div>
          <div style={{ textAlign: "center", fontSize: 36, fontWeight: 800 }}>{text}</div>
          <div style={{ textAlign: "center", opacity: 0.7, marginTop: 6 }}>@{(text.split(" ")[0] || "studio").toLowerCase()}</div>
          <div style={{ display: "flex", justifyContent: "space-around", marginTop: "auto", paddingTop: 20 }}>
            {[["124k", "views"], ["8.2k", "likes"]].map(([n, l]) => (
              <div key={l} style={{ textAlign: "center" }}>
                <div style={{ fontSize: 28, fontWeight: 800, color: GOLD }}>{n}</div>
                <div style={{ opacity: 0.6, fontSize: 16 }}>{l}</div>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </BitsStage>
  );
};

export const SpotlightCardLook: React.FC<BitsCardProps> = ({ text = "SPOTLIGHT" }) => {
  const { frame, durationInFrames } = useClock();
  const x = interpolate(frame, [0, durationInFrames], [10, 90]);
  const y = 30 + Math.sin(frame / 18) * 18;
  return (
    <BitsStage>
      <Card style={{
        background: `radial-gradient(420px 280px at ${x}% ${y}%, ${GOLD}55, rgba(20,20,28,0.92) 55%)`,
        justifyContent: "flex-end",
      }}>
        <div style={{ fontSize: 40, fontWeight: 800 }}>{text}</div>
        <div style={{ opacity: 0.7, marginTop: 8 }}>A light that travels on the clock.</div>
      </Card>
    </BitsStage>
  );
};

export const GlassSurfaceLook: React.FC<BitsCardProps> = ({ text = "GLASS" }) => (
  <BitsStage>
    <Card style={{
      background: "linear-gradient(135deg, rgba(255,255,255,0.22), rgba(255,255,255,0.05))",
      border: "1px solid rgba(255,255,255,0.4)",
      justifyContent: "center", alignItems: "center",
    }}>
      <div style={{ fontSize: 48, fontWeight: 800, color: GOLD }}>{text}</div>
    </Card>
  </BitsStage>
);

export const TiltedCardLook: React.FC<BitsCardProps> = ({ text = "TILT" }) => {
  const { frame } = useClock();
  const rx = Math.sin(frame / 16) * 12;
  const ry = Math.cos(frame / 20) * 16;
  return (
    <BitsStage>
      <div style={{ perspective: 900 }}>
        <Card style={{
          transform: `rotateX(${rx}deg) rotateY(${ry}deg)`,
          justifyContent: "center", alignItems: "center",
        }}>
          <div style={{ fontSize: 48, fontWeight: 800, color: GOLD }}>{text}</div>
        </Card>
      </div>
    </BitsStage>
  );
};

export const CardSwapLook: React.FC<BitsCardProps> = ({ labels = ["One", "Two"] }) => {
  const { frame, durationInFrames } = useClock();
  const a = labels[0] || "One";
  const b = labels[1] || "Two";
  const t = interpolate(frame, [durationInFrames * 0.2, durationInFrames * 0.7], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.inOut(Easing.cubic),
  });
  const card = (label: string, front: boolean) => (
    <Card width={340} height={420} style={{
      position: "absolute",
      transform: front
        ? `translate(${(0.5 - t) * 220}px, 0) scale(${1 - t * 0.08})`
        : `translate(${(t - 0.5) * 220}px, 0) scale(${0.92 + t * 0.08})`,
      zIndex: front ? (t < 0.5 ? 2 : 1) : (t < 0.5 ? 1 : 2),
      justifyContent: "center", alignItems: "center",
    }}>
      <div style={{ fontSize: 44, fontWeight: 800, color: GOLD }}>{label}</div>
    </Card>
  );
  return (
    <BitsStage>
      <div style={{ position: "relative", width: 420, height: 420 }}>
        {card(a, true)}
        {card(b, false)}
      </div>
    </BitsStage>
  );
};

export const CardStackLook: React.FC<BitsCardProps> = ({ labels = ["One", "Two", "Three"] }) => {
  const { frame, fps } = useClock();
  const items = labels.slice(0, 4);
  return (
    <BitsStage>
      <div style={{ position: "relative", width: 380, height: 480 }}>
        {items.map((label, i) => {
          const p = interpolate(frame, [i * 4, i * 4 + fps * 0.4], [0, 1], {
            extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.out(Easing.cubic),
          });
          const n = items.length;
          return (
            <Card key={i} width={360} height={440} style={{
              position: "absolute", left: 10 + i * 10, top: (n - 1 - i) * 18,
              transform: `translateY(${(1 - p) * 30}px) rotate(${(i - 1) * 3}deg)`,
              opacity: p, zIndex: i,
              justifyContent: "center", alignItems: "center",
            }}>
              <div style={{ fontSize: 40, fontWeight: 800, color: GOLD }}>{label}</div>
            </Card>
          );
        })}
      </div>
    </BitsStage>
  );
};

export const CircularCarouselLook: React.FC<BitsCardProps> = ({ labels = ["One", "Two", "Three"] }) => {
  const { frame } = useClock();
  const items = labels.slice(0, 5);
  const n = Math.max(1, items.length);
  return (
    <BitsStage>
      <div style={{ position: "relative", width: 720, height: 720 }}>
        {items.map((label, i) => {
          const ang = (i / n) * Math.PI * 2 + (frame / 40);
          const x = Math.cos(ang) * 240;
          const y = Math.sin(ang) * 150;
          const depth = (Math.sin(ang) + 1) / 2;
          return (
            <Card key={i} width={200} height={260} style={{
              position: "absolute", left: 260 + x, top: 230 + y,
              transform: `scale(${0.72 + depth * 0.32})`,
              zIndex: Math.round(depth * 10),
              opacity: 0.55 + depth * 0.45,
              justifyContent: "center", alignItems: "center",
            }}>
              <div style={{ fontSize: 28, fontWeight: 800, color: GOLD, textAlign: "center" }}>{label}</div>
            </Card>
          );
        })}
      </div>
    </BitsStage>
  );
};

export const BounceCardsLook: React.FC<BitsCardProps> = ({ labels = ["One", "Two", "Three"] }) => {
  const { frame, fps } = useClock();
  const items = labels.slice(0, 3);
  return (
    <BitsStage>
      <div style={{ display: "flex", gap: 18, alignItems: "flex-end" }}>
        {items.map((label, i) => {
          const t0 = 6 + i * 6;
          const y = interpolate(frame, [t0, t0 + fps * 0.45], [220, 0], {
            extrapolateLeft: "clamp", extrapolateRight: "clamp",
            easing: Easing.out(Easing.exp),
          });
          return (
            <Card key={i} width={210} height={280} style={{
              transform: `translateY(${y}px)`,
              justifyContent: "center", alignItems: "center",
            }}>
              <div style={{ fontSize: 28, fontWeight: 800, color: GOLD, textAlign: "center" }}>{label}</div>
            </Card>
          );
        })}
      </div>
    </BitsStage>
  );
};

export const HoloCardLook: React.FC<BitsCardProps> = ({ text = "HOLO" }) => {
  const { frame } = useClock();
  return (
    <BitsStage>
      <Card style={{
        background: `linear-gradient(${frame * 3}deg, #ff9ad5, ${GOLD}, #7af0ff, #b794ff, #ff9ad5)`,
        justifyContent: "center", alignItems: "center",
      }}>
        <div style={{
          width: "100%", height: "100%", borderRadius: 20,
          background: "rgba(10,10,16,0.55)",
          display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: 56, fontWeight: 800, color: "#fff",
          textShadow: `0 0 24px ${GOLD}`,
        }}>{text}</div>
      </Card>
    </BitsStage>
  );
};

export const BitsCounterLook: React.FC<BitsCountProps> = ({ to = 10000, color = GOLD }) => {
  const { frame, durationInFrames } = useClock();
  const n = interpolate(frame, [0, durationInFrames * 0.75], [0, to], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.out(Easing.cubic),
  });
  return (
    <BitsStage>
      <Card height={360} style={{ justifyContent: "center", alignItems: "center" }}>
        <div style={{ opacity: 0.6, letterSpacing: 3, fontSize: 16, marginBottom: 12 }}>COUNT</div>
        <div style={{
          fontSize: 88, fontWeight: 800, color, fontVariantNumeric: "tabular-nums",
        }}>{Math.round(n).toLocaleString("en-US")}</div>
      </Card>
    </BitsStage>
  );
};
