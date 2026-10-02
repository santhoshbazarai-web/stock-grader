// Two-stage justified P/B sensitivity (SPEC §5.6): one table per terminal growth g,
// normalised ROE down the rows, cost of equity across the columns. Cells above the
// current price are green, below red; the base case is outlined.
import { inr, pct } from "@/lib/format";
import type { Valuation } from "@/lib/types";

type Cell = Valuation["justified_pb_grid"][number];

const near = (a: number, b: number | null | undefined) => b != null && Math.abs(a - b) < 1e-9;

export function JustifiedPbGrid({
  inputs,
  cells,
  cmp,
}: {
  inputs: Valuation["justified_pb_inputs"];
  cells: Cell[];
  cmp: number | null;
}) {
  const uniq = (k: "roe" | "g" | "ke") => [...new Set(cells.map((c) => c[k]))].sort((a, b) => a - b);
  const gs = uniq("g");
  const roes = uniq("roe");
  const kes = uniq("ke");
  const at = (roe: number, g: number, ke: number) =>
    cells.find((c) => near(c.roe, roe) && near(c.g, g) && near(c.ke, ke))?.value ?? null;

  return (
    <div className="flex flex-col gap-3">
      {inputs && (
        <p className="text-muted-foreground text-xs">
          Base: ROE {pct(inputs.roe, 1)} → normalised {pct(inputs.normalised_roe, 1)} over{" "}
          {inputs.stage1_years ?? "—"}y, then g {pct(inputs.g, 1)}; Ke {pct(inputs.ke, 2)}
          {inputs.retention != null ? `; ${pct(inputs.retention, 0)} of profit retained` : ""}.
          Override the normalised ROE under Assumptions.
        </p>
      )}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        {gs.map((g) => (
          <table key={g} className="w-full text-xs tabular-nums" aria-label={`terminal growth ${pct(g, 1)}`}>
            <caption className="text-muted-foreground pb-1 text-left">g = {pct(g, 1)}</caption>
            <thead>
              <tr>
                <th className="text-muted-foreground py-0.5 text-left font-normal">ROE ↓ · Ke →</th>
                {kes.map((ke) => (
                  <th key={ke} className="text-muted-foreground py-0.5 text-right font-normal">
                    {pct(ke, 1)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {roes.map((roe) => (
                <tr key={roe} className="border-t">
                  <th className="text-muted-foreground py-0.5 text-left font-normal">{pct(roe, 1)}</th>
                  {kes.map((ke) => {
                    const val = at(roe, g, ke);
                    const base =
                      near(roe, inputs?.normalised_roe) && near(g, inputs?.g) && near(ke, inputs?.ke);
                    const tone =
                      val == null || cmp == null ? "" : val >= cmp ? "text-emerald-600" : "text-red-600";
                    return (
                      <td key={ke} className={`py-0.5 text-right ${tone} ${base ? "font-semibold outline outline-1" : ""}`}>
                        {inr(val)}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        ))}
      </div>
    </div>
  );
}
