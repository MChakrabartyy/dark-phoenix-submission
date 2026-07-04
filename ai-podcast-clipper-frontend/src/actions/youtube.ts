"use server";

import { v4 as uuidv4 } from "uuid";
import { db } from "~/server/db";
import { auth } from "~/server/auth";
import { processVideo } from "./generation";

// Accepts youtube.com/watch?v=, youtu.be/, youtube.com/shorts/ and extracts the 11-char video id.
const YOUTUBE_URL_REGEX =
  /^(?:https?:\/\/)?(?:www\.|m\.)?(?:youtube\.com\/(?:watch\?v=|shorts\/)|youtu\.be\/)([A-Za-z0-9_-]{11})(?:[?&#].*)?$/;

export async function submitYouTubeUrl(url: string): Promise<{
  success: boolean;
  error?: string;
}> {
  const session = await auth();
  if (!session) throw new Error("Unauthorized");

  const match = YOUTUBE_URL_REGEX.exec(url.trim());
  if (!match?.[1]) {
    return {
      success: false,
      error:
        "That doesn't look like a YouTube video link. Paste a link like https://www.youtube.com/watch?v=...",
    };
  }
  const youtubeVideoId = match[1];

  // Mirror generateUploadUrl's key shape exactly so everything downstream
  // (Inngest listing, Modal processing) works identically to a file upload.
  const s3Key = `${uuidv4()}/original.mp4`;

  const uploadedFile = await db.uploadedFile.create({
    data: {
      userId: session.user.id,
      s3Key,
      displayName: `YouTube: ${youtubeVideoId}`,
      uploaded: false,
      sourceUrl: url.trim(),
      youtubeVideoId,
    },
    select: { id: true },
  });

  // Reuses the existing pipeline unchanged: fires the same Inngest event.
  // The Inngest function performs the actual server-side download (via the
  // Modal download endpoint) before processing, so this action returns fast.
  await processVideo(uploadedFile.id);

  return { success: true };
}
