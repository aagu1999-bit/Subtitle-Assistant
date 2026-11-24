import React from "react";
import { interpolate } from "remotion";
import { BitsStage, GOLD, MINT, useClock } from "./shared";

const Blob: React.FC<{ x: number; y: number; size: number; color: string; opacity?: number }> = ({
  x, y, size, color, opacity = 0.55,
}) => (
  <div style={{
    position: "absolute", left: `${x}%`, top: `${y}%`, width: size, height: size,
    marginLeft: -size / 2, marginTop: -size / 2, borderRadius: "50%",
    background: color, opacity, filter: "blur(2px)",
  }} />
);

export const AuroraLook: React.FC = () => {
  const { frame } = useClock();
  const t = frame / 20;
  return (
    <BitsStage opaque>
      <Blob x={30 + Math.sin(t) * 12} y={28 + Math.cos(t * 0.7) * 8} size={720} color="#123d8a" />
      <Blob x={68 + Math.cos(t * 0.8) * 10} y={40 + Math.sin(t * 1.1) * 10} size={640} color="#0a6b5a" />
      <Blob x={50 + Math.sin(t * 0.5) * 8} y={62} size={700} color="#5b2a8a" opacity={0.45} />
      <Blob x={42} y={36 + Math.sin(t) * 6} size={380} color={GOLD} opacity={0.18} />
    </BitsStage>
  );
};

export const SoftAuroraLook: React.FC = () => {
  const { frame } = useClock();
  const t = frame / 28;
  return (
    <BitsStage opaque>
      <AbsoluteFillSoft>
        <Blob x={40 + Math.sin(t) * 8} y={35} size={900} color="#24406a" opacity={0.7} />
        <Blob x={62} y={48 + Math.cos(t) * 6} size={820} color="#2a5a48" opacity={0.55} />
        <Blob x={48} y={58} size={700} color="#4a3a68" opacity={0.4} />
      </AbsoluteFillSoft>
    </BitsStage>
  );
};

const AbsoluteFillSoft: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div style={{ position: "absolute", inset: 0 }}>{children}</div>
);

export const SilkLook: React.FC = () => {
  const { frame } = useClock();
  const ang = 20 + Math.sin(frame / 30) * 12;
  return (
    <BitsStage opaque>
      <div style={{
        position: "absolute", inset: -80,
        background: `repeating-linear-gradient(${ang}deg, #1a1430 0 18px, #2b2150 18px 36px, #c9a22733 36px 40px)`,
        opacity: 0.9,
      }} />
      <div style={{
        position: "absolute", inset: 0,
        background: "radial-gradient(circle at 50% 40%, transparent 20%, #07070c 80%)",
      }} />
    </BitsStage>
  );
};

export const BeamsLook: React.FC = () => {
  const { frame } = useClock();
  return (
    <BitsStage opaque>
      {Array.from({ length: 12 }, (_, i) => {
        const rot = i * 15 + frame * 0.4;
        return (
          <div key={i} style={{
            position: "absolute", left: "50%", top: -80, width: 40, height: 1400,
            marginLeft: -20, transform: `rotate(${rot}deg)`, transformOrigin: "50% 0%",
            background: `linear-gradient(180deg, ${GOLD}55, transparent 70%)`,
            opacity: 0.35 + 0.2 * Math.sin(frame / 10 + i),
          }} />
        );
      })}
    </BitsStage>
  );
};

export const ParticlesLook: React.FC = () => {
  const { frame, durationInFrames } = useClock();
  return (
    <BitsStage>
      {Array.from({ length: 48 }, (_, i) => {
        const x0 = (i * 37) % 100;
        const y0 = (i * 53) % 100;
        const drift = interpolate(frame, [0, durationInFrames], [0, 18 + (i % 7)]);
        const tw = 0.35 + 0.65 * (0.5 + 0.5 * Math.sin(frame / 8 + i));
        return (
          <div key={i} style={{
            position: "absolute",
            left: `${x0}%`,
            top: `${(y0 + drift) % 110 - 5}%`,
            width: 4 + (i % 3) * 2,
            height: 4 + (i % 3) * 2,
            borderRadius: 99,
            background: i % 5 === 0 ? GOLD : "#fff",
            opacity: tw,
          }} />
        );
      })}
    </BitsStage>
  );
};

export const LightningLook: React.FC = () => {
  const { frame } = useClock();
  const flash = frame % 28 < 5 ? 1 : frame % 28 < 8 ? 0.35 : 0;
  const bolt = "M540 80 L500 360 L560 360 L480 820";
  return (
    <BitsStage>
      <svg viewBox="0 0 1080 1920" width={1080} height={1920} style={{ position: "absolute", inset: 0 }}>
        <path d={bolt} fill="none" stroke={GOLD} strokeWidth={10} opacity={flash}
          strokeLinejoin="round" strokeLinecap="round" />
        <path d="M540 80 L580 400 L520 400 L620 900" fill="none" stroke="#fff" strokeWidth={4}
          opacity={flash * 0.8} />
      </svg>
      <div style={{
        position: "absolute", inset: 0, background: "#cfe8ff", opacity: flash * 0.12,
      }} />
    </BitsStage>
  );
};

export const LiquidChromeLook: React.FC = () => {
  const { frame } = useClock();
  return (
    <BitsStage opaque>
      <div style={{
        position: "absolute", inset: -40,
        background: `conic-gradient(from ${frame * 3}deg, #c0c6d0, ${GOLD}, #8a93a3, #f4f7ff, #c0c6d0)`,
      }} />
      <div style={{
        position: "absolute", inset: 0,
        background: "radial-gradient(circle at 50% 45%, transparent 10%, #07070c 78%)",
      }} />
    </BitsStage>
  );
};

export const GalaxyLook: React.FC = () => {
  const { frame } = useClock();
  return (
    <BitsStage opaque>
      <Blob x={50} y={42} size={900} color="#1a1040" opacity={0.9} />
      <Blob x={58} y={38} size={420} color="#4b2a8a" opacity={0.5} />
      <Blob x={40} y={48} size={280} color={GOLD} opacity={0.12} />
      {Array.from({ length: 60 }, (_, i) => {
        const tw = 0.3 + 0.7 * (0.5 + 0.5 * Math.sin(frame / 6 + i));
        return (
          <div key={i} style={{
            position: "absolute",
            left: `${(i * 17) % 100}%`,
            top: `${(i * 29) % 100}%`,
            width: 2 + (i % 3), height: 2 + (i % 3), borderRadius: 99,
            background: "#fff", opacity: tw,
          }} />
        );
      })}
    </BitsStage>
  );
};

export const LightRaysLook: React.FC = () => {
  const { frame } = useClock();
  return (
    <BitsStage>
      {Array.from({ length: 9 }, (_, i) => (
        <div key={i} style={{
          position: "absolute", left: "50%", top: -200, width: 70, height: 1600,
          marginLeft: -35,
          transform: `rotate(${-40 + i * 10 + Math.sin(frame / 20) * 3}deg)`,
          transformOrigin: "50% 0%",
          background: `linear-gradient(180deg, ${GOLD}66, transparent 75%)`,
          opacity: 0.28 + 0.15 * Math.sin(frame / 12 + i),
        }} />
      ))}
    </BitsStage>
  );
};

export const PlasmaLook: React.FC = () => {
  const { frame } = useClock();
  const t = frame / 16;
  return (
    <BitsStage opaque>
      <Blob x={35 + Math.sin(t) * 18} y={40 + Math.cos(t * 0.8) * 12} size={760} color="#d61a7a" opacity={0.55} />
      <Blob x={65 + Math.cos(t) * 14} y={50 + Math.sin(t * 1.2) * 10} size={700} color="#1a6ad6" opacity={0.5} />
      <Blob x={50 + Math.sin(t * 0.6) * 10} y={32} size={560} color={MINT} opacity={0.28} />
    </BitsStage>
  );
};
