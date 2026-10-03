package dev.forge.agent;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.speech.RecognizerIntent;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.text.InputType;
import android.util.Base64;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import org.json.JSONObject;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

public class MainActivity extends Activity {
    private static final String ORIGIN = "https://forge.local";
    private WebView web;
    private SharedPreferences prefs;
    private final ExecutorService pool = Executors.newFixedThreadPool(4);
    private volatile String runnerUrl = "", runnerToken = "";
    private DeviceRunner deviceRunner;
    private volatile boolean devicePaired = false;
    private ValueCallback<Uri[]> fileChooser;
    private static final int FILE_CHOOSER_REQUEST = 1001;
    private static final int VOICE_REQUEST = 1002;
    private String voiceCallbackId;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("pairing", MODE_PRIVATE);
        deviceRunner = new DeviceRunner();
        runnerUrl = prefs.getString("url", "");
        try { runnerToken = decrypt(prefs.getString("token", "")); }
        catch (Exception e) { runnerToken = ""; }
        web = new WebView(this);
        web.setBackgroundColor(Color.rgb(16,20,19));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setBlockNetworkLoads(true); // Only bundled assets render; network requests use the scoped native bridge.
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        if (Build.VERSION.SDK_INT >= 26) s.setSafeBrowsingEnabled(true);
        // File uploads (<input type=file>) need an explicit chooser: without this the attach button silently does nothing.
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileChooser != null) fileChooser.onReceiveValue(null);
                fileChooser = callback;
                Intent intent = params.createIntent();
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                try {
                    startActivityForResult(Intent.createChooser(intent, "Select file"), FILE_CHOOSER_REQUEST);
                } catch (Exception e) {
                    fileChooser = null;
                    return false;
                }
                return true;
            }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return !request.getUrl().toString().equals(ORIGIN + "/index.html");
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                Map<String,String> allowed = new HashMap<>();
                allowed.put(ORIGIN + "/index.html", "text/html");
                allowed.put(ORIGIN + "/app.js", "application/javascript");
                allowed.put(ORIGIN + "/shared.js", "application/javascript");
                allowed.put(ORIGIN + "/cloud.js", "application/javascript");
                allowed.put(ORIGIN + "/style.css", "text/css");
                try {
                    if (allowed.containsKey(url)) {
                        InputStream input = getAssets().open(url.substring(ORIGIN.length()+1));
                        WebResourceResponse response = new WebResourceResponse(allowed.get(url), "UTF-8", input);
                        Map<String,String> headers = new HashMap<>();
                        headers.put("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'none'; object-src 'none'; base-uri 'none'; frame-src 'none'");
                        headers.put("X-Content-Type-Options", "nosniff");
                        response.setResponseHeaders(headers);
                        return response;
                    }
                } catch (Exception ignored) { }
                return new WebResourceResponse("text/plain", "UTF-8", 403, "Blocked", new HashMap<>(), new ByteArrayInputStream(new byte[0]));
            }
        });
        web.addJavascriptInterface(new Bridge(), "ForgeNative");
        setContentView(web);
        // The phone itself is the default computer: start the on-device runner unless disabled.
        // A baked-in shared backend URL (web/shared.js) replaces the device runner entirely:
        // the app silently provisions itself on the owner's Railway instead.
        if (prefs.getBoolean("device_runner", true) && readSharedUrl().isEmpty()) {
            pool.execute(() -> {
                try {
                    deviceRunner.start(this);
                    // Auto-pair when no external runner was configured by the user.
                    if (prefs.getString("url", "").isEmpty()) pairDevice();
                } catch (Exception e) {
                    runOnUiThread(() -> Toast.makeText(this, "On-device runner failed to start: " + e.getMessage(), Toast.LENGTH_LONG).show());
                }
            });
        }
        // Target 35 uses edge-to-edge; reserve system-bar and keyboard insets.
        web.setOnApplyWindowInsetsListener((v, insets) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
                v.setPadding(bars.left,bars.top,bars.right,bars.bottom);
            } else {
                v.setPadding(insets.getSystemWindowInsetLeft(),insets.getSystemWindowInsetTop(),insets.getSystemWindowInsetRight(),insets.getSystemWindowInsetBottom());
            }
            return insets.consumeSystemWindowInsets();
        });
        web.loadUrl(ORIGIN + "/index.html");
    }

    private SecretKey key() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore"); ks.load(null);
        if (!ks.containsAlias("forge-pairing")) {
            KeyGenerator kg = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            kg.init(new KeyGenParameterSpec.Builder("forge-pairing", KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());
            kg.generateKey();
        }
        return (SecretKey) ks.getKey("forge-pairing", null);
    }
    String encrypt(String value) throws Exception {
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding"); c.init(Cipher.ENCRYPT_MODE,key());
        return Base64.encodeToString(c.getIV(),Base64.NO_WRAP) + ":" + Base64.encodeToString(c.doFinal(value.getBytes(StandardCharsets.UTF_8)),Base64.NO_WRAP);
    }
    String decrypt(String value) throws Exception {
        if (value.isEmpty()) return "";
        String[] parts = value.split(":",2);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE,key(),new GCMParameterSpec(128,Base64.decode(parts[0],Base64.NO_WRAP)));
        return new String(c.doFinal(Base64.decode(parts[1],Base64.NO_WRAP)),StandardCharsets.UTF_8);
    }

    static boolean privateHost(String host) {
        if (host == null) return false;
        if (host.equals("localhost") || host.equals("127.0.0.1") || host.equals("[::1]") || host.equals("::1")) return true;
        if (!host.matches("[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+")) return false;
        try {
            String[] p = host.split("\\."); int[] a = new int[4];
            for (int i=0;i<4;i++) { a[i]=Integer.parseInt(p[i]); if (a[i]>255) return false; }
            // Tailscale uses the shared 100.64.0.0/10 range. HTTP still requires explicit trusted-network opt-in.
            return a[0]==10 || (a[0]==192 && a[1]==168) || (a[0]==172 && a[1]>=16 && a[1]<=31)
                || (a[0]==100 && a[1]>=64 && a[1]<=127);
        } catch (Exception e) { return false; }
    }

    private void configureRunner() {
        LinearLayout layout = new LinearLayout(this); layout.setOrientation(LinearLayout.VERTICAL);
        int pad = (int)(24*getResources().getDisplayMetrics().density); layout.setPadding(pad,12,pad,0);
        TextView note = new TextView(this); note.setText("Enter your computer/server address and the pairing token printed by the Forge runner."); layout.addView(note);
        EditText url = new EditText(this); url.setHint("https://runner.example.com:8787"); url.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI); url.setText(runnerUrl); layout.addView(url);
        EditText token = new EditText(this); token.setHint("Pairing token (24+ characters)"); token.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD); token.setText(runnerToken); layout.addView(token);
        CheckBox http = new CheckBox(this); http.setText("Allow HTTP on my trusted LAN or Tailscale network"); http.setChecked(prefs.getBoolean("http",false)); layout.addView(http);
        TextView detail = new TextView(this); detail.setText("HTTP sends tasks and keys without encryption. Use HTTPS for public connections. The pairing token is encrypted with Android Keystore."); detail.setTextSize(12); layout.addView(detail);
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle("Connect your runner").setView(layout).setNegativeButton("Cancel",null).setPositiveButton("Save & connect",null).create();
        dialog.setOnShowListener(d -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
            try {
                String candidate = url.getText().toString().trim().replaceAll("/+$", ""); URI uri = new URI(candidate);
                boolean https = "https".equals(uri.getScheme());
                boolean localHttp = "http".equals(uri.getScheme()) && http.isChecked() && privateHost(uri.getHost());
                if ((!https && !localHttp) || uri.getHost()==null || uri.getUserInfo()!=null || uri.getQuery()!=null || uri.getFragment()!=null || (uri.getPath()!=null && !uri.getPath().isEmpty()))
                    throw new IllegalArgumentException("Use an HTTPS server origin, or enable HTTP for a private IP. No path or embedded credentials.");
                String value = token.getText().toString().trim(); if (value.length()<24) throw new IllegalArgumentException("Pairing token must contain at least 24 characters.");
                String encrypted = encrypt(value);
                prefs.edit().putString("url",candidate).putString("token",encrypted).putBoolean("http",http.isChecked()).apply();
                runnerUrl=candidate; runnerToken=value; dialog.dismiss();
                web.evaluateJavascript("window.onNativeConfigured && window.onNativeConfigured()",null);
            } catch (Exception e) { Toast.makeText(this,e instanceof IllegalArgumentException ? e.getMessage() : "Could not save secure connection settings.",Toast.LENGTH_LONG).show(); }
        }));
        dialog.show();
    }

    private void callback(String id, int code, String payload) {
        runOnUiThread(() -> { if (!isFinishing() && !isDestroyed())
            web.evaluateJavascript("window.onNativeResponse("+JSONObject.quote(id)+","+code+","+JSONObject.quote(payload)+")",null); });
    }
    private class Bridge {        @JavascriptInterface public void deviceAction(String id,String action) {
            pool.execute(() -> {
                try {
                    if ("start".equals(action)) {
                        deviceRunner.start(MainActivity.this);
                        prefs.edit().putBoolean("device_runner", true).apply();
                        String saved = prefs.getString("url", "");
                        if (saved.isEmpty() || saved.equals(deviceRunner.url())) pairDevice();
                    } else if ("stop".equals(action)) {
                        deviceRunner.stop();
                        prefs.edit().putBoolean("device_runner", false).apply();
                        if (devicePaired) { devicePaired = false; }
                    } else throw new IllegalArgumentException("Unknown device action.");
                    callback(id,200,deviceRunner.status(devicePaired).toString());
                } catch(Exception e){callback(id,400,errorMessage(e));}
            });
        }
        @JavascriptInterface public void deviceStatus(String id) {
            pool.execute(() -> {
                try { callback(id,200,deviceRunner.status(devicePaired).toString()); }
                catch(Exception e){callback(id,400,errorMessage(e));}
            });
        }
        @JavascriptInterface public void discoverModels(String id,String profile) {
            pool.execute(() -> {
                try { callback(id,200,ProviderDiscovery.discover(new JSONObject(profile)).toString()); }
                catch(Exception e){callback(id,400,errorMessage(e));}
            });
        }
        @JavascriptInterface public void testProvider(String id,String profile) {
            pool.execute(() -> {
                try { callback(id,200,ProviderDiscovery.testConnection(new JSONObject(profile)).toString()); }
                catch(Exception e){callback(id,400,errorMessage(e));}
            });
        }
        @JavascriptInterface public String loadProviderVault() {
            try {return decrypt(prefs.getString("provider-vault",""));}catch(Exception e){return "";}
        }
        @JavascriptInterface public boolean saveProviderVault(String text) {
            try {if(text.length()>200000)return false;return prefs.edit().putString("provider-vault",encrypt(text)).commit();}
            catch(Exception ignored){return false;}
        }
        @JavascriptInterface public void openExternal(String value) {
            try {
                URI u=new URI(value);
                if(!"https".equals(u.getScheme()) || !"github.com".equals(u.getHost()) || u.getUserInfo()!=null)return;
                runOnUiThread(() -> {
                    try {startActivity(new android.content.Intent(android.content.Intent.ACTION_VIEW,android.net.Uri.parse(value)));}
                    catch(Exception e){Toast.makeText(MainActivity.this,"No browser is available.",Toast.LENGTH_SHORT).show();}
                });
            }catch(Exception ignored){}
        }
        @JavascriptInterface public void configure() { runOnUiThread(() -> configureRunner()); }
        @JavascriptInterface public void voiceInput(String id) {
            runOnUiThread(() -> {
                try {
                    Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
                    intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
                    intent.putExtra(RecognizerIntent.EXTRA_PROMPT, "Speak now");
                    voiceCallbackId = id;
                    startActivityForResult(intent, VOICE_REQUEST);
                } catch (Exception e) { voiceCallbackId = null; callback(id, 400, "{\"error\":\"Voice input is not available on this device.\"}"); }
            });
        }
        @JavascriptInterface public void provisionShared(String id, String value) {
            // Silent first-launch provisioning on the owner's shared Railway backend.
            // value: {"url":"https://...","device":"<id>"}. Saves the per-device token like a pairing.
            pool.execute(() -> {
                HttpURLConnection c = null;
                try {
                    JSONObject v = new JSONObject(value);
                    String url = v.getString("url"), device = v.getString("device");
                    if (!device.matches("[A-Za-z0-9_-]{8,64}")) throw new IllegalArgumentException("Bad device id.");
                    URI u = new URI(url);
                    if (!"https".equals(u.getScheme()) || u.getHost() == null || u.getUserInfo() != null
                            || u.getQuery() != null || u.getFragment() != null
                            || !(u.getPath() == null || u.getPath().isEmpty() || u.getPath().equals("/")))
                        throw new IllegalArgumentException("Shared backend URL must be a plain https URL.");
                    String base = "https://" + u.getHost() + (u.getPort() == -1 ? "" : ":" + u.getPort());
                    c = (HttpURLConnection) new URL(base + "/api/provision").openConnection();
                    c.setInstanceFollowRedirects(false); c.setConnectTimeout(12000); c.setReadTimeout(30000);
                    c.setRequestMethod("POST"); c.setRequestProperty("Content-Type", "application/json");
                    byte[] body = new JSONObject().put("device", device).toString().getBytes(StandardCharsets.UTF_8);
                    c.setDoOutput(true); c.setFixedLengthStreamingMode(body.length);
                    try (java.io.OutputStream out = c.getOutputStream()) { out.write(body); }
                    int status = c.getResponseCode();
                    if (status >= 300 && status < 400) throw new IllegalArgumentException("Backend redirects are blocked.");
                    InputStream src = status >= 400 ? c.getErrorStream() : c.getInputStream();
                    if (src == null) throw new java.io.IOException("No response");
                    ByteArrayOutputStream out = new ByteArrayOutputStream(); byte[] buf = new byte[8192]; int n;
                    try (InputStream in = src) { while ((n = in.read(buf)) != -1) { if (out.size() + n > 65536) throw new java.io.IOException("Response too large"); out.write(buf, 0, n); } }
                    JSONObject r = new JSONObject(out.toString("UTF-8"));
                    if (status != 200) throw new IllegalArgumentException(r.optString("error", "Provisioning failed."));
                    String token = r.getString("token");
                    if (token.length() < 32) throw new IllegalArgumentException("Bad token from backend.");
                    prefs.edit().putString("url", base).putString("token", encrypt(token)).apply();
                    runnerUrl = base; runnerToken = token;
                    callback(id, 200, "{\"ok\":true}");
                    runOnUiThread(() -> web.evaluateJavascript("window.onNativeConfigured && window.onNativeConfigured()", null));
                } catch (Exception e) { callback(id, 400, errorMessage(e)); }
                finally { if (c != null) c.disconnect(); }
            });
        }
        @JavascriptInterface public void request(String id, String path, String method, String body) {
            final String base=runnerUrl, secret=runnerToken;
            if (base.isEmpty() || secret.isEmpty()) { callback(id,400,"{\"error\":\"Open connection settings to pair your runner.\"}"); return; }
            if (!path.startsWith("/api/") || path.contains("#") || path.contains("\\") || !(method.equals("GET") || method.equals("POST")) || body.length()>2_000_000) {
                callback(id,400,"{\"error\":\"Invalid runner request.\"}"); return;
            }
            pool.execute(() -> {
                HttpURLConnection c=null;
                try {
                    c=(HttpURLConnection)new URL(base+path).openConnection();
                    c.setInstanceFollowRedirects(false); c.setConnectTimeout(12000); c.setReadTimeout(65000);
                    c.setRequestMethod(method); c.setRequestProperty("Authorization","Bearer "+secret); c.setRequestProperty("Content-Type","application/json");
                    if (method.equals("POST")) { c.setDoOutput(true); byte[] bytes=body.getBytes(StandardCharsets.UTF_8); c.setFixedLengthStreamingMode(bytes.length); try (java.io.OutputStream out=c.getOutputStream()) { out.write(bytes); } }
                    int status=c.getResponseCode();
                    if (status>=300 && status<400) { callback(id,400,"{\"error\":\"Runner redirects are blocked. Enter the final server URL.\"}"); return; }
                    InputStream source=status>=400 ? c.getErrorStream() : c.getInputStream();
                    if (source==null) throw new java.io.IOException("No response");
                    ByteArrayOutputStream out=new ByteArrayOutputStream(); byte[] bytes=new byte[8192]; int n;
                    try (InputStream in=source) { while ((n=in.read(bytes))!=-1) { if (out.size()+n>8_000_000) throw new java.io.IOException("Response too large"); out.write(bytes,0,n); } }
                    callback(id,status,out.toString("UTF-8"));
                } catch (Exception e) { callback(id,503,"{\"error\":\"Cannot reach runner. Check URL, network, firewall, and TLS certificate.\"}"); }
                finally { if (c!=null) c.disconnect(); }
            });
        }
    }
    private String readSharedUrl() {
        // Owner-baked shared backend URL from the shared.js asset. Empty = normal per-user setup.
        try (InputStream in = getAssets().open("shared.js")) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096]; int n;
            while ((n = in.read(buf)) != -1) out.write(buf, 0, n);
            java.util.regex.Matcher m = java.util.regex.Pattern.compile("FORGE_SHARED_URL\\s*=\\s*'([^']*)'").matcher(out.toString("UTF-8"));
            if (m.find()) return m.group(1).trim();
        } catch (Exception ignored) {}
        return "";
    }
    private String errorMessage(Exception e) {
        try{return new JSONObject().put("error",e instanceof IllegalArgumentException?e.getMessage():"Connection failed. Check network, endpoint, and credentials.").toString();}
        catch(Exception ignored){return "{\"error\":\"Request failed\"}";}
    }
    void pairDevice() throws Exception {
        String url = deviceRunner.url(), token = deviceRunner.token();
        if (!deviceRunner.isRunning() || token.length() < 24)
            throw new IllegalArgumentException("On-device runner is not running.");
        prefs.edit().putString("url", url).putString("token", encrypt(token)).putBoolean("http", true).apply();
        runnerUrl = url; runnerToken = token; devicePaired = true;
        runOnUiThread(() -> web.evaluateJavascript("window.onNativeConfigured && window.onNativeConfigured()", null));
    }
    void pairCloud(String url,String token) throws Exception {
        URI u=new URI(url);
        if(!"http".equals(u.getScheme()) || !privateHost(u.getHost()) || u.getPort()!=8787 || u.getUserInfo()!=null || u.getQuery()!=null || u.getFragment()!=null || !(u.getPath()==null || u.getPath().isEmpty()))
            throw new IllegalArgumentException("Invalid cloud runner address.");
        if(token.length()<24)throw new IllegalArgumentException("Invalid pairing token.");
        prefs.edit().putString("url",url).putString("token",encrypt(token)).putBoolean("http",true).apply();
        runnerUrl=url;runnerToken=token;
        runOnUiThread(() -> web.evaluateJavascript("window.onNativeConfigured && window.onNativeConfigured()",null));
    }
    void notifyCloudChanged() { web.evaluateJavascript("window.onCloudConfigured && window.onCloudConfigured()",null); }
    @Override protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == VOICE_REQUEST) {
            String id = voiceCallbackId; voiceCallbackId = null;
            if (id != null) {
                if (resultCode == Activity.RESULT_OK && data != null) {
                    ArrayList<String> results = data.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS);
                    String text = (results != null && !results.isEmpty()) ? results.get(0) : "";
                    callback(id, 200, JSONObject.quote(text));
                } else {
                    callback(id, 400, "{\"error\":\"Voice input was cancelled.\"}");
                }
            }
            return;
        }
        if (requestCode != FILE_CHOOSER_REQUEST || fileChooser == null) return;
        ValueCallback<Uri[]> callback = fileChooser;
        fileChooser = null;
        if (resultCode != Activity.RESULT_OK || data == null) { callback.onReceiveValue(null); return; }
        android.content.ClipData clip = data.getClipData();
        if (clip != null && clip.getItemCount() > 0) {
            int n = Math.min(clip.getItemCount(), 32);
            Uri[] uris = new Uri[n];
            for (int i = 0; i < n; i++) uris[i] = clip.getItemAt(i).getUri();
            callback.onReceiveValue(uris);
        } else if (data.getData() != null) {
            callback.onReceiveValue(new Uri[]{data.getData()});
        } else {
            callback.onReceiveValue(null);
        }
    }
    @Override protected void onDestroy() { pool.shutdownNow(); try { deviceRunner.stop(); } catch (Exception ignored) {} web.removeJavascriptInterface("ForgeNative"); web.destroy(); super.onDestroy(); }
}
