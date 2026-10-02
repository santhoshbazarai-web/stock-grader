"use client";

// DataTable: sortable columns (click a header; aria-sort), sticky header, tabular numbers.
import { useMemo, useState } from "react";

import { cn } from "@/lib/utils";

export type Column<T> = {
  key: string;
  header: string;
  render: (row: T) => React.ReactNode;
  /** value to sort by; omit to make the column unsortable */
  sort?: (row: T) => number | string | null;
  align?: "left" | "right";
};

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  caption,
  maxHeight = "28rem",
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  caption: string;
  maxHeight?: string;
}) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [dir, setDir] = useState<1 | -1>(1);
  const sorted = useMemo(() => {
    const col = columns.find((c) => c.key === sortKey);
    if (!col?.sort) return rows;
    const f = col.sort;
    return [...rows].sort((a, b) => {
      const x = f(a);
      const y = f(b);
      if (x == null) return 1; // missing values last, whichever the direction
      if (y == null) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * dir;
    });
  }, [rows, columns, sortKey, dir]);
  return (
    <div className="overflow-auto rounded-lg border" style={{ maxHeight }}>
      <table className="w-full text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-muted sticky top-0 z-10">
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                scope="col"
                aria-sort={sortKey === c.key ? (dir === 1 ? "ascending" : "descending") : c.sort ? "none" : undefined}
                className={cn("px-3 py-2 font-medium whitespace-nowrap", c.align === "right" ? "text-right" : "text-left")}
              >
                {c.sort ? (
                  <button
                    type="button"
                    className="hover:text-foreground inline-flex items-center gap-1"
                    onClick={() => {
                      if (sortKey === c.key) setDir((d) => (d === 1 ? -1 : 1));
                      else {
                        setSortKey(c.key);
                        setDir(1);
                      }
                    }}
                  >
                    {c.header}
                    <span aria-hidden className="text-muted-foreground text-[10px]">
                      {sortKey === c.key ? (dir === 1 ? "▲" : "▼") : "↕"}
                    </span>
                  </button>
                ) : (
                  c.header
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={rowKey(r)} className="border-t">
              {columns.map((c) => (
                <td key={c.key} className={cn("tnum px-3 py-1.5", c.align === "right" && "text-right")}>
                  {c.render(r)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
