package dev.forge.agent;

import android.content.Context;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import java.io.File;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.concurrent.atomic.AtomicBoolean;
import org.json.JSONObject;

/** Runs the Forge Python runner on this device via Chaquopy. No RDP or cloud machine needed. */
final class DeviceRunner {
    private static final int PORT = 8787;
    private static boolean pythonStarted = false;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private volatile String token = "";
    private volatile String workspacePath = "";

    synchronized void start(Context context) throws Exception {
        if (running.get()) return;
        if (!pythonStarted) {
            Python.start(new AndroidPlatform(context));
            pythonStarted = true;
        }
        File workspace = new File(context.getFilesDir(), "forge-workspace");
        File skills = new File(context.getFilesDir(), "forge-skills");
        workspacePath = workspace.getAbsolutePath();
        byte[] bytes = new byte[32];
        new SecureRandom().nextBytes(bytes);
        StringBuilder sb = new StringBuilder();
        for (byte b : bytes) sb.append("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789".charAt((b & 0xFF) % 62));
        token = sb.toString();
        Python py = Python.getInstance();
        try {
            py.getModule("runner.device").callAttr("start_device_server", workspacePath, skills.getAbsolutePath(), token);
        } catch (Exception e) {
            throw new IllegalStateException("On-device Python failed to start: " + e.getMessage());
        }
        // Wait for the local server to answer its health check.
        Exception last = null;
        for (int i = 0; i < 40; i++) {
            try {
                if (healthy()) { running.set(true); return; }
            } catch (Exception e) { last = e; }
            try { Thread.sleep(500); } catch (InterruptedException ie) { Thread.currentThread().interrupt(); break; }
        }
        try { py.getModule("runner.device").callAttr("stop_device_server"); } catch (Exception ignored) {}
        throw new IllegalStateException("On-device runner did not start." + (last != null ? " " + last.getMessage() : ""));
    }

    private boolean healthy() throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL("http://127.0.0.1:" + PORT + "/api/health").openConnection();
        c.setConnectTimeout(2000); c.setReadTimeout(2000);
        c.setRequestProperty("Authorization", "Bearer " + token);
        try {
            if (c.getResponseCode() != 200) return false;
            byte[] body = readFully(c.getInputStream());
            return new JSONObject(new String(body, StandardCharsets.UTF_8)).optString("name").equals("Forge Agent");
        } finally { c.disconnect(); }
    }

    // InputStream.readAllBytes() needs API 33; minSdk is 26, so read manually.
    private static byte[] readFully(java.io.InputStream in) throws java.io.IOException {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) != -1) out.write(buf, 0, n);
        return out.toByteArray();
    }

    synchronized void stop() {
        if (!running.get()) return;
        try { Python.getInstance().getModule("runner.device").callAttr("stop_device_server"); }
        catch (Exception ignored) {}
        running.set(false);
        token = "";
    }

    boolean isRunning() { return running.get(); }
    String token() { return token; }
    String url() { return "http://127.0.0.1:" + PORT; }
    String workspacePath() { return workspacePath; }

    JSONObject status(boolean paired) throws Exception {
        return new JSONObject()
            .put("running", running.get())
            .put("url", url())
            .put("workspace", workspacePath)
            .put("paired", paired);
    }
}
