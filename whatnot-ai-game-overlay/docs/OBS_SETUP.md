# OBS + Sony ZV-1 setup

Works with **OBS Studio 28 or newer** (obs-websocket v5 is built in). No OBS plugin needed.

## 1. Camera (Sony ZV-1) in OBS

1. Connect the ZV-1 either
   - via an **HDMI capture card** (recommended: clean 1080p; set the camera's HDMI info display
     off), or
   - via **USB** with the camera's *USB Streaming* mode (720p).
2. In OBS: **Sources → + → Video Capture Device**, name it e.g. `Sony ZV-1`, choose the device.
3. In that source's properties **untick “Deactivate when not showing”** — otherwise OBS stops the
   camera in other scenes and the app receives black frames.
4. Camera tips for recognition: ZV-1 *Product Showcase Setting* **On** (focuses on objects held
   up instead of your face), avoid strong glare on shiny cases, hold games 30–60 cm from the lens.

## 2. Enable the OBS WebSocket server

1. OBS → **Tools → WebSocket Server Settings**.
2. Tick **Enable WebSocket server**. Port **4455** (default).
3. Keep **Enable Authentication** ticked. Click **Show Connect Info** and copy the *Server
   Password*.
4. In the dashboard: **Settings → Credentials → OBS WebSocket password** (or the setup wizard,
   step 1). Host `127.0.0.1`, port `4455` (Settings → OBS & Camera).
5. The top bar should show **OBS connected**.

The app only uses read-only requests (`GetVersion`, `GetInputList`, `GetSourceActive`,
`GetSourceScreenshot`). It never starts/stops streaming or recording and never changes scenes or
settings.

## 3. Choose the camera source and presentation area

Dashboard → **Settings → OBS & Camera** (or wizard step 3):

1. **Camera source** → pick `Sony ZV-1` (marked *(camera)*). Browser sources are not selectable
   — the overlay can never be fed back into recognition.
2. **Rotation** if the camera is mounted sideways.
3. **Presentation area**: drag the rectangle around where you hold games (the rest of the frame
   is ignored). Tighter is better for accuracy and cost.
4. With nothing in front of the camera, click **Capture now** (*Empty presentation area*). The
   app then recognizes "nothing presented" locally, without a Claude call.

## 4. Add the overlay as a Browser Source

1. Check your canvas: OBS → **Settings → Video → Base (Canvas) Resolution**
   (Whatnot vertical: **1080 × 1920**).
2. In your Whatnot scene: **Sources → + → Browser**, name it `Price-O-Matic`.
3. Properties:
   - **URL:** `http://127.0.0.1:8765/overlay`
   - **Width / Height:** your canvas size, e.g. **1080 / 1920** (horizontal: 1920 / 1080)
   - **Custom CSS:** leave the OBS default
     (`body { background-color: rgba(0, 0, 0, 0); margin: 0px auto; overflow: hidden; }`)
   - **Untick** *Shutdown source when not visible* (keeps the live connection open)
   - **Untick** *Refresh browser when scene becomes active* (not needed; it reconnects itself)
   - Leave *Local file* unticked.
4. Make sure the Browser Source is **above** the camera in the source list.
5. Right-click it → **Transform → Reset Transform** so it covers the full canvas exactly. Do
   **not** crop or resize it in OBS — size and position are set in the dashboard
   (compact/balanced/large; bottom-left/center/right, top, side).
6. The dashboard top bar shows `Overlay … · 1 src` when OBS is connected to the overlay.

The overlay is completely transparent until a verified game is recognized. To check placement
before going live, use the dashboard's **Overlay preview → Design samples** (watermarked) or
run `Start-Demo.cmd` and temporarily point a *test* Browser Source at
`http://127.0.0.1:8766/overlay` (demo port) — remove it before your show.

### Startup order

Start the app (`Start.cmd`) **before** opening OBS, or before switching to the Whatnot scene.
If OBS was opened first, the Browser Source's first page load fails (OBS does not retry the
initial load): right-click the Browser Source → **Refresh** once. After the page has loaded
once, the overlay reconnects to the app on its own — restarting the app mid-show needs no
refresh.

## 5. Whatnot considerations

- Whatnot's app draws its own UI (host name, viewer count, chat, bid button, timer) over parts
  of the vertical video. The default sign sits flush at the top and keeps the product area and
  the bottom (chat/bid area) clear. Check it on a phone in the Whatnot app: if the host name /
  viewer count covers the sign, increase *Distance from edge* (Settings → Display & Overlay)
  until it sits just below them.
- The sign stays on screen between games with its prices cleared. To hide it entirely when no
  game is shown, untick *Keep the panel on screen between games*.
- If you ever restart the app during a show, OBS does not need a refresh — the overlay
  reconnects automatically and shows the cleared sign until the next verified record.

## 6. Horizontal (16:9) streams

Set the Browser Source to 1920 × 1080 (or your canvas). The overlay switches to its wide layout
automatically (or force *Layout* in Settings → Display & Overlay).
