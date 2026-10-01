import type { Metadata } from "next";

import { ReportView } from "@/components/report/report-view";

type Props = { params: Promise<{ symbol: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { symbol } = await params;
  return { title: `${decodeURIComponent(symbol).toUpperCase()} · Stock Grader` };
}

export default async function StockPage({ params }: Props) {
  const { symbol } = await params;
  return <ReportView symbol={decodeURIComponent(symbol).toUpperCase()} />;
}
