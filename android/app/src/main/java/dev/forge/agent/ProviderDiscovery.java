package dev.forge.agent;

import java.net.URI;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Locale;
import org.json.JSONObject;
import org.json.JSONArray;

final class ProviderDiscovery {
    static String base(String value, String protocol) throws Exception {
        String b=value.trim().replaceAll("/+$","");
        URI u=new URI(b);
        if(u.getHost()==null || u.getUserInfo()!=null || u.getQuery()!=null || u.getFragment()!=null ||
            !("https".equals(u.getScheme()) || ("http".equals(u.getScheme()) && MainActivity.privateHost(u.getHost()))))
            throw new IllegalArgumentException("Use HTTPS, or a trusted LAN/Tailscale HTTP address without embedded credentials.");
        boolean endpoint=false;
        for(String suffix:new String[]{"/chat/completions","/api/chat","/api/tags","/messages","/responses","/models"})
            if(b.endsWith(suffix)){b=b.substring(0,b.length()-suffix.length());endpoint=true;break;}
        if(protocol.equals("ollama")) return b.endsWith("/api")?b.substring(0,b.length()-4):b;
        if(protocol.equals("gemini")) {
            b=b.replaceAll(":(streamGenerateContent|generateContent)$","");
            String[] parts=b.split("/");
            if(parts.length>=3 && parts[parts.length-2].equals("models"))
                b=b.substring(0,b.length()-parts[parts.length-1].length()).replaceAll("/+$","");
            return b;
        }
        if(protocol.equals("azure")) return b.replaceAll("/openai(/deployments/.*)?$","");
        return new URI(b).getPath().isEmpty() && !endpoint?b+"/v1":b;
    }
    static String protocol(JSONObject p) {
        String kind=p.optString("protocol",p.optString("kind","anthropic"));
        if(kind.equals("claude-code"))kind="anthropic";
        if(kind.equals("codex-cli"))kind="responses";
        if(kind.equals("chat"))kind="custom";
        if(!kind.equals("anthropic") && !kind.equals("responses") && !kind.equals("custom") && !kind.equals("ollama") && !kind.equals("gemini") && !kind.equals("azure"))
            throw new IllegalArgumentException("Unsupported provider protocol.");
        return kind;
    }
    static String key(String value) {
        value=value.trim();
        if(value.toLowerCase(Locale.ROOT).startsWith("bearer "))value=value.substring(7).trim();
        if(!value.matches("[!-~]*"))throw new IllegalArgumentException("API key contains spaces or unsupported characters. Paste only the key.");
        return value;
    }
    static String headerName(String value) {
        value=value.trim();
        if(!value.matches("[A-Za-z0-9-]+"))throw new IllegalArgumentException("Custom header name must be letters, digits, or dashes.");
        return value;
    }
    static HashMap<String,String> headers(JSONObject p, String protocol) {
        String mode=p.optString("authMode","auto");
        if(mode.equals("auto"))mode=protocol.equals("anthropic")?"api-key":protocol.equals("gemini")?"x-goog-api-key":protocol.equals("azure")?"api-key-header":"bearer";
        if(!mode.equals("none") && !mode.equals("api-key") && !mode.equals("api-key-header") && !mode.equals("bearer") && !mode.equals("x-goog-api-key") && !mode.equals("custom"))
            throw new IllegalArgumentException("Unsupported authentication mode.");
        String key=mode.equals("none")?"":key(p.optString("apiKey",""));
        HashMap<String,String> headers=new HashMap<>();
        if(protocol.equals("anthropic"))headers.put("anthropic-version","2023-06-01");
        if(!key.isEmpty()) {
            if(mode.equals("bearer"))headers.put("Authorization","Bearer "+key);
            else if(mode.equals("api-key"))headers.put("x-api-key",key);
            else if(mode.equals("api-key-header"))headers.put("api-key",key);
            else if(mode.equals("x-goog-api-key"))headers.put("x-goog-api-key",key);
            else headers.put(headerName(p.optString("customHeader","")),key);
        }
        return headers;
    }
    static void checkError(JSONObject data) {
        if(data.has("error") && !data.isNull("error"))throw new IllegalArgumentException("Provider returned an API error. Check the selected model, API key, account quota, and protocol.");
    }
    static JSONObject discover(JSONObject p) throws Exception {
        String protocol=protocol(p), b=base(p.getString("baseUrl"),protocol);
        String listPath = protocol.equals("gemini") ? "/v1beta/models"
            : protocol.equals("azure") ? "/openai/deployments?api-version=2024-08-01"
            : protocol.equals("ollama") ? "/api/tags" : "/models";
        JSONObject data=Net.json(b+listPath,"GET",null,headers(p,protocol));
        checkError(data);
        JSONArray source = protocol.equals("gemini") ? data.optJSONArray("models")
            : protocol.equals("azure") ? data.optJSONArray("value") : data.optJSONArray(protocol.equals("ollama")?"models":"data");
        if (protocol.equals("azure") && source == null) source = data.optJSONArray("data");
        JSONArray models=new JSONArray();
        HashSet<String> seen=new HashSet<>();
        if(source!=null)for(int i=0;i<Math.min(source.length(),1000);i++) {
            JSONObject m=source.optJSONObject(i);if(m==null)continue;
            String id;
            String display;
            if (protocol.equals("gemini")) {
                id=m.optString("name",""); if(id.startsWith("models/"))id=id.substring(7);
                display=m.optString("displayName",m.optString("display_name",id));
            } else if (protocol.equals("azure")) {
                id=m.optString("name",m.optString("id","")); display=m.optString("model",id);
            } else {
                id=m.optString(protocol.equals("ollama")?"name":"id","");
                display=m.optString("display_name",m.optString("name",id));
            }
            if(!id.isEmpty() && seen.add(id))models.put(new JSONObject().put("id",id).put("name",display.isEmpty()?id:display));
        }
        if(models.length()==0)throw new IllegalArgumentException("No models returned. Check the endpoint/key or open Advanced for a provider-specific fallback.");
        return new JSONObject().put("models",models).put("baseUrl",new URI(b).getPath().isEmpty() && !protocol.equals("gemini") && !protocol.equals("azure")?p.getString("baseUrl").trim():b).put("protocol",protocol).put("chatVerified",false);
    }
    static JSONObject testConnection(JSONObject p) throws Exception {
        String protocol=protocol(p), b=base(p.getString("baseUrl"),protocol), model=p.optString("model","").trim();
        if(model.isEmpty())throw new IllegalArgumentException("Select a model or enter a model override before testing.");
        JSONObject schema=new JSONObject().put("type","object").put("properties",new JSONObject());
        JSONObject function=new JSONObject().put("name","connection_check").put("description","Connection test; do not call.").put("parameters",schema);
        JSONArray messages=new JSONArray().put(new JSONObject().put("role","user").put("content","Reply with OK. Do not call tools."));
        JSONObject body=new JSONObject().put("model",model).put("stream",false);
        String path;
        if(protocol.equals("anthropic")) {
            path="/messages";
            body.put("messages",messages).put("max_tokens",256).put("tools",new JSONArray().put(new JSONObject().put("name","connection_check").put("description","Connection test; do not call.").put("input_schema",schema)));
        } else if(protocol.equals("responses")) {
            path="/responses";
            body.put("input",messages).put("store",false).put("max_output_tokens",256).put("tools",new JSONArray().put(new JSONObject(function.toString()).put("type","function").put("strict",false)));
        } else if(protocol.equals("gemini")) {
            path="/v1beta/models/"+model+":generateContent";
            body=new JSONObject()
                .put("system_instruction",new JSONObject().put("parts",new JSONArray().put(new JSONObject().put("text","Reply with OK. Do not call tools."))))
                .put("contents",new JSONArray().put(new JSONObject().put("role","user").put("parts",new JSONArray().put(new JSONObject().put("text","Reply with OK. Do not call tools.")))))
                .put("tools",new JSONArray().put(new JSONObject().put("functionDeclarations",new JSONArray().put(new JSONObject().put("name","connection_check").put("description","Connection test; do not call.").put("parameters",schema)))))
                .put("generationConfig",new JSONObject().put("maxOutputTokens",256));
        } else if(protocol.equals("azure")) {
            path="/openai/deployments/"+model+"/chat/completions?api-version=2024-08-01";
            body.put("messages",new JSONArray().put(new JSONObject().put("role","system").put("content","Reply with OK. Do not call tools.")).put(new JSONObject().put("role","user").put("content","Reply with OK. Do not call tools.")))
                .put("tools",new JSONArray().put(new JSONObject().put("type","function").put("function",function))).put("max_tokens",256);
        } else {
            path=protocol.equals("ollama")?"/api/chat":"/chat/completions";
            body.put("messages",messages).put("tools",new JSONArray().put(new JSONObject().put("type","function").put("function",function)));
            if(protocol.equals("ollama"))body.put("options",new JSONObject().put("num_predict",256));
            else body.put("max_tokens",256);
        }
        JSONObject data=Net.json(b+path,"POST",body,headers(p,protocol),45000);
        checkError(data);
        boolean valid;
        if(protocol.equals("anthropic"))valid=data.optJSONArray("content")!=null && data.getJSONArray("content").length()>0;
        else if(protocol.equals("responses"))valid=data.optJSONArray("output")!=null && !data.optString("status").equals("failed");
        else if(protocol.equals("ollama"))valid=data.optJSONObject("message")!=null;
        else if(protocol.equals("gemini")){JSONArray c=data.optJSONArray("candidates");valid=c!=null && c.length()>0 && c.optJSONObject(0)!=null && c.getJSONObject(0).optJSONObject("content")!=null;}
        else {JSONArray choices=data.optJSONArray("choices");valid=choices!=null && choices.length()>0 && choices.optJSONObject(0)!=null && choices.getJSONObject(0).optJSONObject("message")!=null;}
        if(!valid)throw new IllegalArgumentException("Provider response does not match the selected API protocol. Check the API format selected for this provider.");
        return new JSONObject().put("ok",true).put("model",model).put("protocol",protocol).put("message","Chat API request succeeded. No tools were executed.");
    }
}
