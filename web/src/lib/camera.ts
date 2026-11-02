// Browser camera helpers: open a camera and grab JPEG frames from it.

export async function openCamera(facing: "environment" | "user" = "environment"): Promise<MediaStream> {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error(
      window.isSecureContext
        ? "This browser can't use the camera."
        : "The camera only works over HTTPS (or on localhost). Open Irris through its https:// address.",
    );
  }
  try {
    return await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: facing }, width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false,
    });
  } catch (e: any) {
    if (e?.name === "NotAllowedError") throw new Error("Camera access was blocked. Allow it in the browser's site settings and try again.");
    if (e?.name === "NotFoundError") throw new Error("No camera was found on this device.");
    throw new Error(e?.message || "Couldn't open the camera.");
  }
}

export function stopCamera(stream: MediaStream | null) {
  stream?.getTracks().forEach((t) => t.stop());
}

let canvas: HTMLCanvasElement | null = null;

export function grabJpeg(video: HTMLVideoElement, maxSide = 960, quality = 0.78): Promise<Blob | null> {
  const w = video.videoWidth;
  const h = video.videoHeight;
  if (!w || !h) return Promise.resolve(null);
  const scale = Math.min(1, maxSide / Math.max(w, h));
  canvas = canvas || document.createElement("canvas");
  canvas.width = Math.round(w * scale);
  canvas.height = Math.round(h * scale);
  const ctx = canvas.getContext("2d");
  if (!ctx) return Promise.resolve(null);
  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve) => canvas!.toBlob((b) => resolve(b), "image/jpeg", quality));
}

/** Keep a phone's screen on while it streams. Returns a release function. */
export async function keepAwake(): Promise<() => void> {
  let lock: any = null;
  const nav: any = navigator;
  const request = async () => {
    try {
      lock = await nav.wakeLock?.request("screen");
    } catch {
      /* not supported or refused: the phone may dim, the stream still works while it's on */
    }
  };
  await request();
  const onVis = () => document.visibilityState === "visible" && request();
  document.addEventListener("visibilitychange", onVis);
  return () => {
    document.removeEventListener("visibilitychange", onVis);
    lock?.release?.().catch(() => {});
  };
}

export function speak(text: string) {
  try {
    if (!("speechSynthesis" in window) || !text) return;
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.04;
    window.speechSynthesis.speak(u);
  } catch {
    /* speech is a nicety */
  }
}
