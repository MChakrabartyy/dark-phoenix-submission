"use client";

import Dropzone, { type DropzoneState } from "shadcn-dropzone";
import type { Clip } from "@prisma/client";
import Link from "next/link";
import { Button } from "./ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "./ui/card";
import { Link2, Loader2, UploadCloud, Video, X } from "lucide-react";
import { useState } from "react";
import { generateUploadUrl } from "~/actions/s3";
import { toast } from "sonner";
import { processVideo } from "~/actions/generation";
import { submitYouTubeUrl } from "~/actions/youtube";
import { Input } from "./ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "./ui/table";
import { Badge } from "./ui/badge";
import { useRouter } from "next/navigation";
import { ClipDisplay } from "./clip-display";

export function DashboardClient({
  uploadedFiles,
  clips,
}: {
  uploadedFiles: {
    id: string;
    s3Key: string;
    filename: string;
    status: string;
    clipsCount: number;
    createdAt: Date;
  }[];
  clips: Clip[];
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [youtubeUrl, setYoutubeUrl] = useState("");
  const [submittingUrl, setSubmittingUrl] = useState(false);
  const router = useRouter();

  const handleYouTubeSubmit = async () => {
    if (!youtubeUrl.trim()) return;
    setSubmittingUrl(true);
    try {
      const result = await submitYouTubeUrl(youtubeUrl);
      if (!result.success) {
        toast.error("Invalid YouTube link", { description: result.error });
        return;
      }
      setYoutubeUrl("");
      toast.success("YouTube video queued", {
        description:
          "The video is being downloaded and processed. Check the status below.",
        duration: 5000,
      });
      router.refresh();
    } catch {
      toast.error("Something went wrong", {
        description: "Could not queue that video. Please try again.",
      });
    } finally {
      setSubmittingUrl(false);
    }
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    router.refresh();
    setTimeout(() => setRefreshing(false), 600);
  };

  const handleDrop = (acceptedFiles: File[]) => {
    setFiles(acceptedFiles);
  };

  const handleRemoveFile = () => {
    setFiles([]);
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const handleUpload = async () => {
    if (files.length === 0) return;

    const file = files[0]!;
    setUploading(true);

    try {
      const { success, signedUrl, uploadedFileId } = await generateUploadUrl({
        filename: file.name,
        contentType: file.type,
      });

      if (!success) throw new Error("Failed to get upload URL");

      const uploadResponse = await fetch(signedUrl, {
        method: "PUT",
        body: file,
        headers: {
          "Content-Type": file.type,
        },
      });

      if (!uploadResponse.ok)
        throw new Error(`Upload filed with status: ${uploadResponse.status}`);

      await processVideo(uploadedFileId);

      setFiles([]);

      toast.success("Video uploaded successfully", {
        description:
          "Your video has been scheduled for processing. Check the status below.",
        duration: 5000,
      });
    } catch (error) {
      toast.error("Upload failed", {
        description:
          "There was a problem uploading your video. Please try again.",
      });
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="mx-auto flex max-w-5xl flex-col space-y-6 px-4 py-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Podcast Clipper
          </h1>
          <p className="text-muted-foreground">
            Upload your podcast and get AI-generated clips instantly
          </p>
        </div>
        <Link href="/dashboard/billing">
          <Button>Buy Credits</Button>
        </Link>
      </div>

      <Tabs defaultValue="upload">
        <TabsList>
          <TabsTrigger value="upload">Upload</TabsTrigger>
          <TabsTrigger value="my-clips">My Clips</TabsTrigger>
        </TabsList>

        <TabsContent value="upload">
          <Card>
            <CardHeader>
              <CardTitle>Upload Podcast</CardTitle>
              <CardDescription>
                Upload your audio or video file to generate clips
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="border-input mb-6 border p-4">
                <div className="mb-2 flex items-center gap-2">
                  <Link2 className="text-muted-foreground h-4 w-4" />
                  <p className="text-sm font-medium">
                    Process a YouTube video
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Input
                    type="url"
                    placeholder="https://www.youtube.com/watch?v=..."
                    value={youtubeUrl}
                    onChange={(e) => setYoutubeUrl(e.target.value)}
                    disabled={submittingUrl}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void handleYouTubeSubmit();
                    }}
                  />
                  <Button
                    disabled={!youtubeUrl.trim() || submittingUrl}
                    onClick={handleYouTubeSubmit}
                  >
                    {submittingUrl ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        Queuing...
                      </>
                    ) : (
                      "Generate Clips"
                    )}
                  </Button>
                </div>
                <p className="text-muted-foreground mt-2 text-xs">
                  The video is downloaded server-side - no need to upload
                  anything yourself.
                </p>
              </div>

              <p className="text-muted-foreground mb-2 text-sm font-medium">
                Or upload a file
              </p>
              <Dropzone
                onDrop={handleDrop}
                accept={{ "video/mp4": [".mp4"] }}
                maxSize={500 * 1024 * 1024}
                disabled={uploading}
                maxFiles={1}
              >
                {(dropzone: DropzoneState) => (
                  <>
                    <div className="border-input flex flex-col items-center justify-center space-y-4 border border-dashed p-10 text-center">
                      <UploadCloud className="text-muted-foreground h-8 w-8" />
                      <p className="font-medium">Drag and drop your file</p>
                      <p className="text-muted-foreground text-sm">
                        or click to browse (MP4 up to 500MB)
                      </p>
                      <Button
                        className="cursor-pointer"
                        variant="default"
                        size="sm"
                        disabled={uploading}
                      >
                        Select File
                      </Button>
                    </div>
                  </>
                )}
              </Dropzone>

              {files.length > 0 && (
                <div className="bg-muted relative mt-4 p-3">
                  <div className="absolute top-1 right-1">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="text-muted-foreground hover:text-foreground p-2"
                      aria-label="Remove file"
                      disabled={uploading}
                      onClick={handleRemoveFile}
                    >
                      <X className="size-4 shrink-0" aria-hidden={true} />
                    </Button>
                  </div>
                  <div className="flex items-center space-x-2.5">
                    <span className="ring-input bg-background flex h-10 w-10 shrink-0 items-center justify-center ring-1">
                      <Video
                        className="text-foreground size-5"
                        aria-hidden={true}
                      />
                    </span>
                    <div className="w-full">
                      <p className="text-foreground text-xs font-medium">
                        {files[0]!.name}
                      </p>
                      <p className="text-muted-foreground mt-0.5 flex justify-between text-xs">
                        <span>{formatFileSize(files[0]!.size)}</span>
                        <span>{uploading ? "Uploading..." : "Ready"}</span>
                      </p>
                    </div>
                  </div>
                </div>
              )}

              <div className="mt-4 flex items-center justify-end">
                <Button
                  disabled={files.length === 0 || uploading}
                  onClick={handleUpload}
                >
                  {uploading ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Uploading...
                    </>
                  ) : (
                    "Upload and Generate Clips"
                  )}
                </Button>
              </div>

              {uploadedFiles.length > 0 && (
                <div className="pt-6">
                  <div className="mb-2 flex items-center justify-between">
                    <h3 className="text-md mb-2 font-medium">Queue status</h3>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleRefresh}
                      disabled={refreshing}
                    >
                      {refreshing && (
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      )}
                      Refresh
                    </Button>
                  </div>
                  <div className="max-h-[300px] overflow-auto rounded-md border">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>File</TableHead>
                          <TableHead>Uploaded</TableHead>
                          <TableHead>Status</TableHead>
                          <TableHead>Clips created</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {uploadedFiles.map((item) => (
                          <TableRow key={item.id}>
                            <TableCell className="max-w-xs truncate font-medium">
                              {item.filename}
                            </TableCell>
                            <TableCell className="text-muted-foreground text-sm">
                              {new Date(item.createdAt).toLocaleDateString()}
                            </TableCell>
                            <TableCell>
                              {item.status === "queued" && (
                                <Badge variant="outline">Queued</Badge>
                              )}
                              {item.status === "processing" && (
                                <Badge variant="outline">Processing</Badge>
                              )}
                              {item.status === "processed" && (
                                <Badge variant="outline">Processed</Badge>
                              )}
                              {item.status === "no credits" && (
                                <Badge variant="destructive">No credits</Badge>
                              )}
                              {item.status === "failed" && (
                                <Badge variant="destructive">Failed</Badge>
                              )}
                            </TableCell>
                            <TableCell>
                              {item.clipsCount > 0 ? (
                                <span>
                                  {item.clipsCount} clip
                                  {item.clipsCount !== 1 ? "s" : ""}
                                </span>
                              ) : (
                                <span className="text-muted-foreground">
                                  No clips yet
                                </span>
                              )}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="my-clips">
          <Card>
            <CardHeader>
              <CardTitle>My Clips</CardTitle>
              <CardDescription>
                View and manage your generated clips here. Processing may take a
                few minuntes.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <ClipDisplay clips={clips} />
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
