"use client";

// Treemap (Recharts): group → stock, area = market cap, colour = the chosen measure. Click a
// group header to zoom into it, a stock to open its page. Loaded lazily by the page.
import { useRouter } from "next/navigation";
import { ResponsiveContainer, Treemap } from "recharts";

import { signedPct } from "@/lib/format";
import { fillFor, textOn, type ColourBy, type MapRow, type TreeNode } from "@/lib/valuation-map";

type Props = {
  tree: TreeNode[];
  colourBy: ColourBy;
  onZoom: (group: string) => void;
  onHover: (r: MapRow | null) => void;
};

type CellProps = { x: number; y: number; width: number; height: number; depth: number; name: string; row?: MapRow; colourBy: ColourBy; onZoom: (g: string) => void; onHover: (r: MapRow | null) => void; open: (s: string) => void };

function Cell({ x, y, width, height, depth, name, row, colourBy, onZoom, onHover, open }: CellProps) {
  if (depth === 1) {
    // group frame + header (clickable: zoom)
    return (
      <g onClick={() => onZoom(name)} style={{ cursor: "zoom-in" }}>
        <rect x={x} y={y} width={width} height={height} fill="none" stroke="var(--viz-surface)" strokeWidth={3} />
        {width > 60 && height > 18 && (
          <text x={x + 6} y={y + 13} fontSize={11} fontWeight={600} fill="var(--foreground)" style={{ paintOrder: "stroke", stroke: "var(--background)", strokeWidth: 3 }}>
            {name.length * 6.5 > width ? `${name.slice(0, Math.max(3, Math.floor(width / 6.5) - 1))}…` : name}
          </text>
        )}
      </g>
    );
  }
  if (!row) return null;
  const fill = fillFor(row, colourBy);
  const ink = textOn(fill);
  const value = colourBy === "grade" ? row.grade ?? "—" : colourBy === "day_change" ? signedPct(row.day_change_pct, 1) : signedPct(row.discount_pct, 0);
  return (
    <g
      onClick={() => open(row.symbol)}
      onMouseEnter={() => onHover(row)}
      onMouseLeave={() => onHover(null)}
      role="link"
      aria-label={`${row.symbol}: ${value}`}
      style={{ cursor: "pointer" }}
    >
      <rect x={x} y={y} width={width} height={height} fill={fill} stroke="var(--viz-surface)" strokeWidth={1.5} />
      {width > 44 && height > 28 && (
        <>
          <text x={x + 5} y={y + 14} fontSize={11} fontWeight={600} fill={ink}>
            {row.symbol}
          </text>
          <text x={x + 5} y={y + 27} fontSize={10} fill={ink}>
            {value}
          </text>
        </>
      )}
    </g>
  );
}

export default function ValuationTreemap({ tree, colourBy, onZoom, onHover }: Props) {
  const router = useRouter();
  return (
    <div className="h-[460px] w-full" data-testid="valuation-treemap">
      <ResponsiveContainer>
        <Treemap
          data={tree as never}
          dataKey="size"
          isAnimationActive={false}
          content={((p: Omit<CellProps, "colourBy" | "onZoom" | "onHover" | "open">) => (
            <Cell {...p} colourBy={colourBy} onZoom={onZoom} onHover={onHover} open={(s) => router.push(`/stocks/${s}`)} />
          )) as never}
        />
      </ResponsiveContainer>
    </div>
  );
}
