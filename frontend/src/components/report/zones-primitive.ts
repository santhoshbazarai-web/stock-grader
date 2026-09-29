// Series primitive that draws price rectangles (demand/supply zones, the buy zone) behind the
// candles. Each box spans from its start bar to its end bar (or the right edge when open).
import type {
  IChartApi,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";
import type { CanvasRenderingTarget2D } from "fancy-canvas";

export type Box = {
  from: Time;
  to: Time | null; // null = extend to the right edge
  top: number;
  bottom: number;
  fill: string;
  stroke: string;
};

class BoxesRenderer implements IPrimitivePaneRenderer {
  constructor(
    private boxes: Box[],
    private chart: IChartApi,
    private series: ISeriesApi<SeriesType>,
  ) {}

  draw() {}

  drawBackground(target: CanvasRenderingTarget2D) {
    target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio: hr, verticalPixelRatio: vr, bitmapSize }) => {
      const ts = this.chart.timeScale();
      for (const b of this.boxes) {
        const x1 = ts.timeToCoordinate(b.from);
        const x2 = b.to == null ? bitmapSize.width / hr : ts.timeToCoordinate(b.to);
        const y1 = this.series.priceToCoordinate(b.top);
        const y2 = this.series.priceToCoordinate(b.bottom);
        if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
        const left = Math.round(Math.min(x1, x2) * hr);
        const width = Math.max(1, Math.round(Math.abs(x2 - x1) * hr));
        const top = Math.round(Math.min(y1, y2) * vr);
        const height = Math.max(1, Math.round(Math.abs(y2 - y1) * vr));
        ctx.fillStyle = b.fill;
        ctx.fillRect(left, top, width, height);
        ctx.strokeStyle = b.stroke;
        ctx.lineWidth = Math.max(1, Math.round(hr));
        ctx.strokeRect(left + 0.5, top + 0.5, width - 1, height - 1);
      }
    });
  }
}

class BoxesView implements IPrimitivePaneView {
  constructor(private source: BoxesPrimitive) {}
  zOrder() {
    return "bottom" as const;
  }
  renderer() {
    const { chart, series } = this.source;
    return chart && series ? new BoxesRenderer(this.source.boxes, chart, series) : null;
  }
}

export class BoxesPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType> | null = null;
  private views: IPrimitivePaneView[];
  private requestUpdate: (() => void) | null = null;

  constructor(public boxes: Box[]) {
    this.views = [new BoxesView(this)];
  }

  attached(param: SeriesAttachedParameter<Time>) {
    this.chart = param.chart as IChartApi;
    this.series = param.series as ISeriesApi<SeriesType>;
    this.requestUpdate = param.requestUpdate;
  }

  detached() {
    this.chart = null;
    this.series = null;
  }

  setBoxes(boxes: Box[]) {
    this.boxes = boxes;
    this.requestUpdate?.();
  }

  paneViews() {
    return this.views;
  }
}
