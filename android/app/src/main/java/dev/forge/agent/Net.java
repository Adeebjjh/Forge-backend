package dev.forge.agent;

import java.net.HttpURLConnection;
import java.net.URL;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import org.json.JSONObject;

final class Net {
    static byte[] read(InputStream in, int limit) throws Exception {
        if (in == null) return new byte[0];
        try (InputStream stream=in; ByteArrayOutputStream out=new ByteArrayOutputStream()) {
            byte[] buf=new byte[8192]; int n;
            while ((n=stream.read(buf))!=-1) {
                if (out.size()+n>limit) throw new IllegalArgumentException("Response is too large.");
                out.write(buf,0,n);
            }
            return out.toByteArray();
        }
    }
    static JSONObject json(String url, String method, JSONObject body, Map<String,String> headers) throws Exception {
        return json(url, method, body, headers, 30000);
    }
    static String httpError(int code) {
        if(code==401)return "Authentication rejected (HTTP 401). Re-enter the API key/token and check its authentication header. Model discovery alone does not prove chat access.";
        if(code==403)return "Access denied (HTTP 403). Check account, model permissions, and IP restrictions.";
        if(code==404 || code==405)return "API route or model not found (HTTP "+code+"). Check the API base URL and selected protocol.";
        if(code==429)return "Provider limit reached (HTTP 429). Check quota, credit, or rate limits.";
        if(code>=300 && code<400)return "Request redirected. Enter the final API URL; credentials are never forwarded to redirects.";
        return "HTTP "+code+". Check endpoint, account access, and service status.";
    }
    static JSONObject parseObject(String text) throws Exception {
        text=text.trim();
        if(text.startsWith("\uFEFF"))text=text.substring(1).trim();
        if(text.isEmpty())throw new IllegalArgumentException("The endpoint returned an empty response. Check the API URL.");
        if(text.startsWith("<"))throw new IllegalArgumentException("The endpoint returned HTML instead of JSON (a website, login page, or gateway challenge). Enter the provider's API base URL.");
        if(text.startsWith("data:") || text.startsWith("event:"))throw new IllegalArgumentException("The endpoint returned a stream instead of the requested JSON. Check the API protocol.");
        try {return new JSONObject(text);}
        catch(org.json.JSONException e){throw new IllegalArgumentException("The endpoint returned invalid JSON. Check its API URL and protocol.");}
    }
    static JSONObject json(String url, String method, JSONObject body, Map<String,String> headers, int readTimeout) throws Exception {
        HttpURLConnection c=(HttpURLConnection)new URL(url).openConnection();
        try {
            c.setInstanceFollowRedirects(false); c.setConnectTimeout(12000); c.setReadTimeout(readTimeout);
            c.setRequestMethod(method); c.setRequestProperty("User-Agent","Forge-Agent/0.3");
            c.setRequestProperty("Content-Type","application/json");
            c.setRequestProperty("Accept","application/json");
            for (Map.Entry<String,String> h:headers.entrySet()) c.setRequestProperty(h.getKey(),h.getValue());
            if (body!=null) {
                c.setDoOutput(true); byte[] bytes=body.toString().getBytes(StandardCharsets.UTF_8);
                c.setFixedLengthStreamingMode(bytes.length);
                try (java.io.OutputStream out=c.getOutputStream()) { out.write(bytes); }
            }
            int code=c.getResponseCode();
            if (code<200 || code>=300) throw new IllegalArgumentException(httpError(code));
            if (code==204 || code==205) return new JSONObject();
            String text=new String(read(c.getInputStream(),4000000),StandardCharsets.UTF_8);
            return parseObject(text);
        } finally { c.disconnect(); }
    }
    static byte[] artifact(String url, Map<String,String> headers) throws Exception {
        if (!url.startsWith("https://api.github.com/repos/")) throw new IllegalArgumentException("Invalid artifact URL.");
        HttpURLConnection c=(HttpURLConnection)new URL(url).openConnection();
        String redirect;
        try {
            c.setInstanceFollowRedirects(false); c.setConnectTimeout(12000); c.setReadTimeout(30000);
            for(Map.Entry<String,String> h:headers.entrySet()) c.setRequestProperty(h.getKey(),h.getValue());
            int code=c.getResponseCode();
            if(code==200) return read(c.getInputStream(),1000000);
            if(code!=302 && code!=307) throw new IllegalArgumentException("Artifact HTTP "+code);
            redirect=c.getHeaderField("Location");
        } finally { c.disconnect(); }
        if(redirect==null || !redirect.startsWith("https://")) throw new IllegalArgumentException("Invalid download redirect.");
        c=(HttpURLConnection)new URL(redirect).openConnection();
        try {
            c.setInstanceFollowRedirects(false); c.setConnectTimeout(12000); c.setReadTimeout(30000);
            // GitHub credentials must never follow the signed storage redirect.
            if(c.getResponseCode()!=200) throw new IllegalArgumentException("Session download failed.");
            return read(c.getInputStream(),1000000);
        } finally { c.disconnect(); }
    }
}
