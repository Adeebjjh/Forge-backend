package dev.forge.agent;

import org.json.JSONObject;
import org.junit.Test;
import static org.junit.Assert.*;

public class ProviderDiscoveryTest {
    @Test public void customPathsArePreserved() throws Exception {
        for(String path:new String[]{"/api","/openai","/gateway/v2","/v1beta","/anthropic/v1"}) {
            assertEquals("https://example.com"+path,ProviderDiscovery.base("https://example.com"+path,"custom"));
            assertEquals("https://example.com"+path,ProviderDiscovery.base("https://example.com"+path+"/chat/completions","custom"));
        }
        assertEquals("https://example.com/v1",ProviderDiscovery.base("https://example.com","custom"));
        assertEquals("https://example.com",ProviderDiscovery.base("https://example.com/chat/completions","custom"));
        assertEquals("http://127.0.0.1:11434",ProviderDiscovery.base("http://127.0.0.1:11434/api/chat","ollama"));
    }
    @Test public void pastedBearerKeyIsNotPrefixedTwice() throws Exception {
        JSONObject p=new JSONObject().put("apiKey"," \nBearer test-key\r\n").put("authMode","bearer");
        assertEquals("Bearer test-key",ProviderDiscovery.headers(p,"custom").get("Authorization"));
        p.put("authMode","api-key");
        assertEquals("test-key",ProviderDiscovery.headers(p,"custom").get("x-api-key"));
        p.put("authMode","api-key-header");
        assertEquals("test-key",ProviderDiscovery.headers(p,"custom").get("api-key"));
        p.put("authMode","none");
        assertTrue(ProviderDiscovery.headers(p,"custom").isEmpty());
    }
    @Test public void unsafeKeyIsRejectedWithoutEchoingIt() {
        try { ProviderDiscovery.key("secret\r\nInjected: bad"); fail("Must reject header injection"); }
        catch(IllegalArgumentException e) { assertFalse(e.getMessage().contains("secret")); }
    }
    @Test public void geminiAndAzureBasesAreNormalized() throws Exception {
        assertEquals("https://generativelanguage.googleapis.com",
            ProviderDiscovery.base("https://generativelanguage.googleapis.com","gemini"));
        assertEquals("https://generativelanguage.googleapis.com/v1beta/models",
            ProviderDiscovery.base("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent","gemini"));
        assertEquals("https://example.openai.azure.com",
            ProviderDiscovery.base("https://example.openai.azure.com","azure"));
        assertEquals("https://example.openai.azure.com",
            ProviderDiscovery.base("https://example.openai.azure.com/openai/deployments/gpt-4o/chat/completions","azure"));
        assertEquals("gemini",ProviderDiscovery.protocol(new JSONObject().put("kind","gemini")));
        assertEquals("azure",ProviderDiscovery.protocol(new JSONObject().put("kind","azure")));
    }
    @Test public void geminiAzureAndCustomHeaders() throws Exception {
        JSONObject p=new JSONObject().put("apiKey","test-key").put("authMode","auto");
        assertEquals("test-key",ProviderDiscovery.headers(p,"gemini").get("x-goog-api-key"));
        assertEquals("test-key",ProviderDiscovery.headers(p,"azure").get("api-key"));
        p.put("authMode","custom").put("customHeader","X-Gateway-Key");
        assertEquals("test-key",ProviderDiscovery.headers(p,"custom").get("X-Gateway-Key"));
        try { ProviderDiscovery.headers(new JSONObject().put("apiKey","k").put("authMode","custom").put("customHeader","Bad Header!"),"custom"); fail("Must reject bad header name"); }
        catch(IllegalArgumentException expected) {}
    }
    @Test public void cliAndApiUseSameProtocol() throws Exception {
        assertEquals("anthropic",ProviderDiscovery.protocol(new JSONObject().put("kind","claude-code")));
        assertEquals("responses",ProviderDiscovery.protocol(new JSONObject().put("kind","codex-cli")));
    }
    @Test public void jsonBomWorksAndBadResponsesAreExplained() throws Exception {
        assertEquals(1,Net.parseObject("\uFEFF{\"ok\":1}").getInt("ok"));
        for(String response:new String[]{"","<html>login</html>","not-json","data: {}","[]"}) {
            try {Net.parseObject(response);fail("Must reject response");}
            catch(IllegalArgumentException e) {assertFalse(e.getMessage().contains("character 1"));}
        }
        assertTrue(Net.httpError(401).contains("does not prove chat access"));
        assertTrue(Net.httpError(302).contains("never forwarded"));
    }
}
