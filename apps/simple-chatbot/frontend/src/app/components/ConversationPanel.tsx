"use client";

import { MessagesSquareIcon } from "lucide-react";
import { useState } from "react";
import type { ConversationMessage } from "@pipecat-ai/client-react";

import { Conversation } from "@/components/pipecat/conversation";
import type { TextRenderMode } from "@/components/pipecat/conversation-message";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

import { Panel, PanelHeader, PanelTitle } from "./Panel";
import { ProtectionNotices } from "./ProtectionNotices";
import type { ChatError } from "@/session";

const RENDER_MODES: { value: TextRenderMode; label: string }[] = [
  { value: "karaoke", label: "Karaoke" },
  { value: "captions", label: "Captions" },
  { value: "instant", label: "Instant" },
];

export function ConversationPanel({
  model,
  notices,
  errors,
  className,
}: {
  model: string;
  notices: string[];
  errors: ChatError[];
  className?: string;
}) {
  const [renderMode, setRenderMode] = useState<TextRenderMode>("karaoke");
  const localMessages: ConversationMessage[] = errors.map(({ text, createdAt }) => ({
    role: "system",
    createdAt,
    final: true,
    parts: [{ text, createdAt, final: true }],
  }));

  return (
    <Panel aria-label="Conversation" className={className}>
      <PanelHeader className="justify-between">
        <PanelTitle className="flex items-center gap-2">
          <MessagesSquareIcon className="size-4" />
          Conversation
        </PanelTitle>
        <Select
          items={RENDER_MODES}
          value={renderMode}
          onValueChange={(value) => {
            if (value) setRenderMode(value);
          }}
        >
          <SelectTrigger size="sm" aria-label="Text render mode">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {RENDER_MODES.map((mode) => (
              <SelectItem key={mode.value} value={mode.value}>
                {mode.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </PanelHeader>
      <Conversation
        assistantLabel={model}
        systemLabel="Error"
        localMessages={localMessages}
        textRenderMode={renderMode}
        className="min-h-0 flex-1"
      />
      <ProtectionNotices notices={notices} />
    </Panel>
  );
}
