package xiaozhi.modules.app.auth;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.*;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import xiaozhi.modules.app.auth.wechat.WechatIdentityClient;

/** Actual embedded Tomcat + Spring + Shiro + Liquibase + isolated MySQL/Redis.
 * Only the WeChat exchange is fake; this is explicitly not real-WeChat acceptance. */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT, properties = {
        "app.auth.enabled=true", "app.auth.wechat-app-id=test-app", "app.auth.wechat-secret=fake-test-secret"})
@ActiveProfiles("dev")
@EnabledIfEnvironmentVariable(named = "MINIAPP_TEST_JDBC_URL", matches = ".+")
class AppApplicationIntegrationTest {
    @Autowired TestRestTemplate http;
    @MockitoBean WechatIdentityClient wechat;
    private final ObjectMapper json = new ObjectMapper();

    private JsonNode login() throws Exception {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        var response = http.exchange("/app/v1/auth/wechat/login", HttpMethod.POST,
                new HttpEntity<>("{\"code\":\"" + UUID.randomUUID() + "\"}", headers), String.class);
        assertEquals(HttpStatus.OK, response.getStatusCode());
        assertEquals("no-store", response.getHeaders().getCacheControl());
        assertNotNull(response.getHeaders().getFirst("X-Request-ID"));
        JsonNode data = json.readTree(response.getBody()).get("data");
        assertTrue(data.get("expires_in").isInt());
        return data;
    }
    private ResponseEntity<String> authorized(String path, HttpMethod method, String token) {
        HttpHeaders headers = new HttpHeaders();
        headers.setBearerAuth(token);
        return http.exchange(path, method, new HttpEntity<>(headers), String.class);
    }
    @Test void actualHttpLoginMeAndLogoutKeepOtherSessionWorking() throws Exception {
        when(wechat.exchange(anyString())).thenReturn("http-test-" + UUID.randomUUID());
        var a = login();
        var b = login();
        assertEquals(a.get("user_id"), b.get("user_id"));
        String tokenA = a.get("access_token").asText();
        String tokenB = b.get("access_token").asText();
        var me = authorized("/app/v1/me", HttpMethod.GET, tokenA);
        assertEquals(HttpStatus.OK, me.getStatusCode());
        assertEquals(a.get("user_id"), json.readTree(me.getBody()).get("data").get("user_id"));
        assertEquals(HttpStatus.OK, authorized("/app/v1/auth/logout", HttpMethod.POST, tokenA).getStatusCode());
        var revoked = authorized("/app/v1/me", HttpMethod.GET, tokenA);
        assertEquals(HttpStatus.UNAUTHORIZED, revoked.getStatusCode());
        assertEquals(61104, json.readTree(revoked.getBody()).get("code").intValue());
        assertEquals(HttpStatus.OK, authorized("/app/v1/me", HttpMethod.GET, tokenB).getStatusCode());
        assertEquals(HttpStatus.UNAUTHORIZED, http.getForEntity("/app/v1/me", String.class).getStatusCode());
    }
}
