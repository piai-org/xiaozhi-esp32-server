package xiaozhi.modules.app.auth;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.Date;
import java.util.Map;
import org.apache.shiro.web.mgt.WebSecurityManager;
import org.apache.shiro.util.ThreadContext;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.springframework.context.support.StaticMessageSource;
import org.springframework.mock.web.MockFilterConfig;
import org.springframework.mock.web.MockServletContext;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.filter.RequestContextFilter;
import jakarta.servlet.Filter;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import xiaozhi.common.utils.MessageUtils;
import xiaozhi.modules.security.user.SecurityUser;
import xiaozhi.common.utils.Result;
import xiaozhi.modules.app.account.AppAccountRepository;
import xiaozhi.modules.app.auth.config.AppAuthProperties;
import xiaozhi.modules.app.auth.controller.*;
import xiaozhi.modules.app.auth.session.*;
import xiaozhi.modules.security.config.*;
import xiaozhi.modules.security.entity.SysUserTokenEntity;
import xiaozhi.modules.security.oauth2.Oauth2Realm;
import xiaozhi.modules.security.service.ShiroService;
import xiaozhi.modules.sys.entity.SysUserEntity;
import xiaozhi.modules.sys.service.SysParamsService;

/** Exercise the actual ordered Shiro chains, plus the production MVC JSON converter. */
class AppFilterChainTest {
    private AppLoginService login;
    private AppLoginCoordinator coordinator;
    private AppSessionService sessions;
    private AppAccountRepository accounts;
    private AppAuthProperties properties;
    private MockMvc mvc;
    private Filter filter;
    private ShiroService adminStore;
    private Object previousMessageSource;
    private static final Instant NOW = Instant.parse("2026-10-21T08:00:00Z");
    private final String appToken = AppTokens.generate();
    private static final String USER = "u_12345678-1234-1234-1234-123456789012";
    private final ObjectMapper json = new ObjectMapper();

    @RestController
    static class LegacyProbe {
        @GetMapping("/private/probe") Result<Map<String, Object>> admin() {
            return new Result<Map<String, Object>>().ok(Map.of("admin_id", SecurityUser.getUserId()));
        }
        @GetMapping("/config/probe") Result<String> server() { return new Result<String>().ok("server"); }
    }
    @BeforeEach void setup() throws Exception {
        properties = new AppAuthProperties();
        properties.setEnabled(true);
        accounts = mock(AppAccountRepository.class);
        when(accounts.findSession(AppTokens.digest(appToken))).thenReturn(new AppAccountRepository.Session(
                "session-1", USER, NOW.plusSeconds(604800), null, 1));
        sessions = new AppSessionService(accounts, properties, Clock.fixed(NOW, ZoneOffset.UTC));
        login = mock(AppLoginService.class);
        when(login.login(anyString(), anyString())).thenReturn(new AppLoginResponse(USER, appToken, 604800,
                NOW.plusSeconds(604800).toString()));
        adminStore = mock(ShiroService.class);
        SysUserTokenEntity token = new SysUserTokenEntity();
        token.setUserId(1L);
        token.setExpireDate(new Date(System.currentTimeMillis() + 600000));
        when(adminStore.getByToken("admin-token")).thenReturn(token);
        SysUserEntity user = new SysUserEntity();
        user.setId(1L);
        user.setStatus(1);
        user.setSuperAdmin(1);
        when(adminStore.getUser(1L)).thenReturn(user);
        Oauth2Realm realm = new Oauth2Realm();
        ReflectionTestUtils.setField(realm, "shiroService", adminStore);
        var shiro = new ShiroConfig();
        WebSecurityManager manager = shiro.securityManager(realm, shiro.sessionManager());
        SysParamsService params = mock(SysParamsService.class);
        when(params.getValue(anyString(), eq(true))).thenReturn("server-secret");
        var factory = ShiroConfig.shirFilter(manager, params, sessions);
        MockServletContext servletContext = new MockServletContext();
        filter = (Filter) factory.getObject();
        filter.init(new MockFilterConfig(servletContext));

        StaticMessageSource messages = new StaticMessageSource();
        messages.setUseCodeAsDefaultMessage(true);
        previousMessageSource = ReflectionTestUtils.getField(MessageUtils.class, "messageSource");
        ReflectionTestUtils.setField(MessageUtils.class, "messageSource", messages);
        coordinator = new AppLoginCoordinator(login);
        mvc = MockMvcBuilders.standaloneSetup(new AppAuthController(coordinator, sessions), new LegacyProbe())
                .setControllerAdvice(new AppExceptionAdvice())
                .setMessageConverters(new WebMvcConfig().jackson2HttpMessageConverter())
                .addFilters(new RequestContextFilter(), filter).build();
    }
    @AfterEach void close() {
        coordinator.close();
        filter.destroy();
        ThreadContext.remove();
        ReflectionTestUtils.setField(MessageUtils.class, "messageSource", previousMessageSource);
    }
    private JsonNode body(MvcResult result) throws Exception { return json.readTree(result.getResponse().getContentAsString()); }

    @Test void onlyExactPostLoginIsAnonymousAndResponseMatchesFrontendContract() throws Exception {
        var result = mvc.perform(post("/xiaozhi/app/v1/auth/wechat/login").contextPath("/xiaozhi")
                .contentType("application/json").content("{\"code\":\"wx-code\",\"user_id\":\"attacker\",\"openid\":\"fake\"}"))
                .andExpect(status().isOk()).andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(header().exists("X-Request-ID")).andReturn();
        JsonNode data = body(result).get("data");
        assertEquals(USER, data.get("user_id").asText());
        assertTrue(data.get("user_id").isTextual());
        assertTrue(data.get("expires_in").isInt());
        assertEquals(604800, data.get("expires_in").intValue());
        assertEquals("Bearer", data.get("token_type").asText());
        assertEquals(NOW.plusSeconds(604800), Instant.parse(data.get("expires_at").asText()));
        assertEquals(5, data.size());
        verify(login).login(eq("wx-code"), anyString());
        mvc.perform(get("/app/v1/auth/wechat/login")).andExpect(status().isUnauthorized());
        mvc.perform(post("/app/v1/auth/wechat/login/extra")).andExpect(status().isUnauthorized());
    }
    @Test void missingTokenAndQueryTokenCannotAccessMe() throws Exception {
        mvc.perform(get("/app/v1/me").param("access_token", appToken).param("user_id", USER))
                .andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value(61101));
        verifyNoInteractions(accounts);
    }
    @Test void appTokenAuthenticatesOnlyAppPrincipalAndMeNeverEchoesCallerIdentity() throws Exception {
        mvc.perform(get("/app/v1/me").param("user_id", "someone-else").header("Authorization", "Bearer " + appToken))
                .andExpect(status().isOk()).andExpect(jsonPath("$.data.user_id").value(USER))
                .andExpect(jsonPath("$.data.openid").doesNotExist()).andExpect(header().doesNotExist("Set-Cookie"));
        assertNull(ThreadContext.getSubject());
        verifyNoInteractions(adminStore);
    }
    @Test void adminAndDeviceAndServerTokensNeverBecomeBusinessUsers() throws Exception {
        for (String token : new String[]{"admin-token", "device-token", "server-secret"})
            mvc.perform(get("/app/v1/me").header("Authorization", "Bearer " + token))
                    .andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value(61102));
        verifyNoInteractions(accounts);
    }
    @Test void appTokenCannotPassManagementOrServerFilterAndOldCredentialsStillWork() throws Exception {
        mvc.perform(get("/private/probe").header("Authorization", "Bearer " + appToken))
                .andExpect(jsonPath("$.code").value(401));
        mvc.perform(get("/config/probe").header("Authorization", "Bearer " + appToken))
                .andExpect(jsonPath("$.code").value(401));
        mvc.perform(get("/private/probe").header("Authorization", "Bearer admin-token"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.data.admin_id").value("1"));
        mvc.perform(get("/config/probe").header("Authorization", "Bearer server-secret"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.data").value("server"));
    }
    @Test void logoutUsesAuthenticatedSessionRatherThanCallerSuppliedUser() throws Exception {
        mvc.perform(post("/app/v1/auth/logout").header("Authorization", "Bearer " + appToken)
                .contentType("application/json").content("{\"user_id\":\"someone-else\"}"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.code").value(0));
        verify(accounts).revoke(eq("session-1"), eq(USER), eq(NOW));
    }
    @ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(strings = {"{}", "{\"code\":null}", "{\"code\":\"\"}",
            "{\"code\":123}", "{\"code\":true}", "{\"code\":[]}", "{\"code\":{}}", "{\"code\":\"a b\"}", "malformed"})
    void malformedLoginNeverReachesWechatExchange(String request) throws Exception {
        mvc.perform(post("/app/v1/auth/wechat/login").contentType("application/json").content(request))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value(61001));
        verifyNoInteractions(login);
    }
    @Test void excessiveCodeLengthIsRejected() throws Exception {
        mvc.perform(post("/app/v1/auth/wechat/login").contentType("application/json")
                .content("{\"code\":\"" + "A".repeat(513) + "\"}"))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value(61001));
        verifyNoInteractions(login);
    }
    @Test void disabledFeatureReturnsUnavailableButLegacyFilterStillWorks() throws Exception {
        properties.setEnabled(false);
        mvc.perform(post("/app/v1/auth/wechat/login").contentType("application/json").content("{\"code\":\"code\"}"))
                .andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.code").value(61007));
        mvc.perform(get("/config/probe").header("Authorization", "Bearer server-secret"))
                .andExpect(status().isOk());
        verifyNoInteractions(login, accounts);
    }
    @Test void dependencyFailureIs503AndExplicitExpiredSessionIs401() throws Exception {
        when(accounts.findSession(anyString())).thenThrow(new org.springframework.dao.DataAccessResourceFailureException("secret"));
        mvc.perform(get("/app/v1/me").header("Authorization", "Bearer " + appToken))
                .andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.code").value(61902));
        doReturn(new AppAccountRepository.Session("id", USER, NOW, null, 1)).when(accounts).findSession(anyString());
        mvc.perform(get("/app/v1/me").header("Authorization", "Bearer " + appToken))
                .andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value(61103));
    }
    @Test void errorsAndObservabilityNeverLogCodeTokenOpenIdOrSecret() throws Exception {
        Logger root = (Logger) org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ListAppender<ILoggingEvent> captured = new ListAppender<>();
        captured.start();
        root.addAppender(captured);
        try {
            when(login.login(anyString(), anyString())).thenThrow(new RuntimeException(
                    "https://api.weixin.qq.com?secret=SECRET-SENTINEL&js_code=CODE-SENTINEL openid=OPENID-SENTINEL " + appToken));
            mvc.perform(post("/app/v1/auth/wechat/login").contentType("application/json")
                    .content("{\"code\":\"CODE-SENTINEL\"}"))
                    .andExpect(status().isInternalServerError()).andExpect(jsonPath("$.code").value(61999));
            String logs = captured.list.stream().map(ILoggingEvent::getFormattedMessage).reduce("", (a, b) -> a + b);
            assertFalse(logs.contains("SECRET-SENTINEL"));
            assertFalse(logs.contains("CODE-SENTINEL"));
            assertFalse(logs.contains("OPENID-SENTINEL"));
            assertFalse(logs.contains(appToken));
            assertTrue(logs.contains("request_id="));
            assertTrue(captured.list.stream().allMatch(event -> event.getThrowableProxy() == null));
        } finally { root.detachAppender(captured); captured.stop(); }
    }
}
