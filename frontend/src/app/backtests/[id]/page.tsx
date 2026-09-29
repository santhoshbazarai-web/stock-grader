import type { Metadata } from "next";

import { BacktestResult } from "@/components/pages/backtest-result";

type Props = { params: Promise<{ id: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  return { title: `Backtest #${id} · Stock Grader` };
}

export default async function BacktestPage({ params }: Props) {
  const { id } = await params;
  return <BacktestResult id={id} />;
}
