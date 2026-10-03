"use client";

import { Page } from "@/components/common";
import { NotesPanel } from "@/components/notes-panel";

export function MyNotes() {
  return (
    <Page>
      <header>
        <h1 className="text-2xl font-semibold">My Notes</h1>
        <p className="text-muted-foreground text-sm">Your research notes across all stocks, newest first.</p>
      </header>
      <NotesPanel />
    </Page>
  );
}
