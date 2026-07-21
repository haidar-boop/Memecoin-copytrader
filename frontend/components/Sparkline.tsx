import React from "react";

export function Sparkline({
  values,
  width = 220,
  height = 48,
  stroke = "#4f9dff",
}: {
  values: Array<number | null | undefined>;
  width?: number;
  height?: number;
  stroke?: string;
}) {
  const nums = values
    .map((v) => (v === null || v === undefined ? NaN : Number(v)))
    .filter((v) => isFinite(v)) as number[];
  if (nums.length < 2) {
    return <div className="text-xs text-muted">not enough data</div>;
  }
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const pad = 3;
  const stepX = (width - pad * 2) / (nums.length - 1);
  const points = nums.map((v, i) => {
    const x = pad + i * stepX;
    const y = pad + (height - pad * 2) * (1 - (v - min) / span);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const last = nums[nums.length - 1];
  const lastPt = points[points.length - 1].split(",");
  return (
    <svg width={width} height={height} className="overflow-visible">
      <polyline
        fill="none"
        stroke={stroke}
        strokeWidth={1.5}
        points={points.join(" ")}
      />
      <circle
        cx={lastPt[0]}
        cy={lastPt[1]}
        r={2.5}
        fill={last >= nums[0] ? "#34d399" : "#f87171"}
      />
    </svg>
  );
}
