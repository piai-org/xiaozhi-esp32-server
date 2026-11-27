package xiaozhi.modules.app.auth;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.core.io.ClassPathResource;
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator;
import xiaozhi.modules.app.account.AppAccountRepository;
import xiaozhi.modules.app.auth.config.AppAuthProperties;
import xiaozhi.modules.app.auth.controller.AppLoginResponse;
import xiaozhi.modules.app.auth.session.AppSessionService;
import xiaozhi.modules.app.auth.session.AppTokens;
import xiaozhi.modules.app.auth.wechat.WechatIdentityClient;

@EnabledIfEnvironmentVariable(named = "MINIAPP_TEST_JDBC_URL", matches = ".+")
class AppMysqlIntegrationTest {
    private static DataSource dataSource;
    private static JdbcTemplate jdbc;
    private AppAccountRepository accounts;
    private AppAuthProperties properties;
    private Clock clock;
    private WechatIdentityClient wechat;
    private LoginGuard guard;
    private AppLoginService login;
    private AppSessionService sessions;
    private static final Instant NOW = Instant.parse("2026-10-21T08:00:00.123Z");

    @BeforeAll static void database() {
        dataSource = MiniappTestDatabase.connect();
        jdbc = new JdbcTemplate(dataSource);
        if (jdbc.queryForObject("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() "
                + "AND table_name='app_user'", Integer.class) == 0)
            new ResourceDatabasePopulator(new ClassPathResource("db/changelog/202610211200.sql")).execute(dataSource);
    }
    @BeforeEach void setup() {
        jdbc.update("DELETE FROM app_session");
        jdbc.update("DELETE FROM app_wechat_identity");
        jdbc.update("DELETE FROM app_user");
        accounts = spy(new AppAccountRepository(dataSource));
        properties = new AppAuthProperties();
        properties.setEnabled(true);
        properties.setWechatAppId("test-app");
        properties.setWechatSecret("fake-secret");
        clock = Clock.fixed(NOW, ZoneOffset.UTC);
        wechat = mock(WechatIdentityClient.class);
        when(wechat.exchange(anyString())).thenReturn("verified-user");
        guard = mock(LoginGuard.class);
        login = service(accounts, wechat);
        sessions = new AppSessionService(accounts, properties, clock);
    }
    private AppLoginService service(AppAccountRepository repo, WechatIdentityClient client) {
        return new AppLoginService(properties, repo, client, guard, clock, new DataSourceTransactionManager(dataSource));
    }
    private int count(String table) { return jdbc.queryForObject("SELECT COUNT(*) FROM " + table, Integer.class); }
    private AppAuthException authFailure(AppError expected, org.junit.jupiter.api.function.Executable action) {
        var failure = assertThrows(AppAuthException.class, action);
        assertEquals(expected, failure.getError());
        return failure;
    }

    @Test void firstLoginWithoutDeviceAndRepeatedLoginHaveStableStringIdentityAndIndependentSessions() {
        var a = login.login("new-code", "proxy");
        var b = login.login("another-code", "proxy");
        assertTrue(a.getUserId().matches("u_[0-9a-f-]{36}"));
        assertEquals(a.getUserId(), b.getUserId());
        assertNotEquals(a.getAccessToken(), b.getAccessToken());
        assertEquals(a.getUserId(), sessions.authenticate(a.getAccessToken()).userId());
        assertEquals(1, count("app_user"));
        assertEquals(1, count("app_wechat_identity"));
        assertEquals(2, count("app_session"));
        assertEquals(604800, a.getExpiresIn());
        assertEquals(NOW.plusSeconds(604800), Instant.parse(a.getExpiresAt()));
        assertEquals(AppTokens.digest(a.getAccessToken()), jdbc.queryForObject(
                "SELECT token_hash FROM app_session WHERE token_hash=?", String.class, AppTokens.digest(a.getAccessToken())));
        assertFalse(jdbc.queryForList("SELECT * FROM app_session").toString().contains(a.getAccessToken()));
    }
    @Test void differentWechatIdentitiesAndCaseSensitiveOpenIdsStaySeparate() {
        when(wechat.exchange("code-a")).thenReturn("OpenID");
        when(wechat.exchange("code-b")).thenReturn("openid");
        var a = login.login("code-a", "proxy");
        var b = login.login("code-b", "proxy");
        assertNotEquals(a.getUserId(), b.getUserId());
        assertEquals(b.getUserId(), sessions.authenticate(b.getAccessToken()).userId());
    }
    @Test void parallelFirstLoginsRollbackLosingUserAndReReadWinnerInFreshTransaction() throws Exception {
        int parallel = 6;
        CyclicBarrier readBarrier = new CyclicBarrier(parallel);
        AtomicInteger reads = new AtomicInteger();
        doAnswer(invocation -> {
            Object result = invocation.callRealMethod();
            if (reads.incrementAndGet() <= parallel) readBarrier.await(2, TimeUnit.SECONDS);
            return result;
        }).when(accounts).findIdentity(anyString(), anyString());
        try (ExecutorService executor = Executors.newFixedThreadPool(parallel)) {
            List<Future<AppLoginResponse>> pending = new ArrayList<>();
            for (int i = 0; i < parallel; i++) {
                final String code = "parallel-" + i;
                pending.add(executor.submit(() -> login.login(code, "proxy")));
            }
            String expected = null;
            for (var future : pending) {
                var result = future.get(12, TimeUnit.SECONDS);
                if (expected == null) expected = result.getUserId();
                assertEquals(expected, result.getUserId());
                assertEquals(expected, sessions.authenticate(result.getAccessToken()).userId());
            }
        }
        assertEquals(1, count("app_user"));
        assertEquals(1, count("app_wechat_identity"));
        assertEquals(parallel, count("app_session"));
        verify(wechat, times(parallel)).exchange(anyString());
    }
    @Test void insertFailureRollsBackAllThreeTablesAndReturnsNoCredentials() {
        doThrow(new DataAccessResourceFailureException("sensitive database details"))
                .when(accounts).createSession(anyString(), anyString(), anyString(), any(), any());
        var failure = authFailure(AppError.DEPENDENCY_UNAVAILABLE, () -> login.login("rollback", "proxy"));
        assertNull(failure.getCause());
        assertEquals(0, count("app_user"));
        assertEquals(0, count("app_wechat_identity"));
        assertEquals(0, count("app_session"));
    }
    @Test void commitFailureDoesNotReturnCredentialsAndRollsBack() {
        DataSourceTransactionManager failing = new DataSourceTransactionManager(dataSource) {
            @Override protected void doCommit(org.springframework.transaction.support.DefaultTransactionStatus status) {
                throw new org.springframework.transaction.TransactionSystemException("commit unavailable");
            }
        };
        failing.setRollbackOnCommitFailure(true);
        var broken = new AppLoginService(properties, accounts, wechat, guard, clock, failing);
        authFailure(AppError.DEPENDENCY_UNAVAILABLE, () -> broken.login("commit-failure", "proxy"));
        assertEquals(0, count("app_user"));
        assertEquals(0, count("app_session"));
    }
    @Test void disabledUserCannotLoginOrUseOldSessionAndIsNeverRecreated() {
        var a = login.login("code-a", "proxy");
        jdbc.update("UPDATE app_user SET status=0 WHERE id=?", a.getUserId());
        authFailure(AppError.USER_DISABLED, () -> sessions.authenticate(a.getAccessToken()));
        authFailure(AppError.USER_DISABLED, () -> login.login("code-b", "proxy"));
        assertEquals(1, count("app_user"));
        assertEquals(1, count("app_session"));
    }
    @Test void logoutOnlyRevokesCurrentSessionAndCleanupDoesNotDeleteUserOrOtherSession() {
        var a = login.login("code-a", "proxy");
        var b = login.login("code-b", "proxy");
        sessions.logout(sessions.authenticate(a.getAccessToken()));
        authFailure(AppError.TOKEN_REVOKED, () -> sessions.authenticate(a.getAccessToken()));
        assertEquals(b.getUserId(), sessions.authenticate(b.getAccessToken()).userId());
        jdbc.update("UPDATE app_session SET revoked_at=? WHERE token_hash=?",
                LocalDateTime.ofInstant(NOW.minusSeconds(8 * 86400), ZoneOffset.UTC), AppTokens.digest(a.getAccessToken()));
        sessions.cleanup();
        assertEquals(1, count("app_session"));
        assertEquals(1, count("app_user"));
        assertEquals(b.getUserId(), sessions.authenticate(b.getAccessToken()).userId());
    }
    @Test void expiryHasFixedBoundaryNoSlidingRenewalAndSurvivesServiceRecreation() {
        var a = login.login("code-a", "proxy");
        new AppSessionService(new AppAccountRepository(dataSource), properties, clock).authenticate(a.getAccessToken());
        var almost = new AppSessionService(accounts, properties, Clock.fixed(NOW.plusSeconds(604799), ZoneOffset.UTC));
        assertEquals(a.getUserId(), almost.authenticate(a.getAccessToken()).userId());
        var expired = new AppSessionService(accounts, properties, Clock.fixed(NOW.plusSeconds(604800), ZoneOffset.UTC));
        authFailure(AppError.TOKEN_EXPIRED, () -> expired.authenticate(a.getAccessToken()));
        assertEquals(a.getExpiresAt(), loginTimestamp(a.getAccessToken()));
    }
    private String loginTimestamp(String token) { return accounts.findSession(AppTokens.digest(token)).expiresAt().toString(); }
    @Test void dependencyFailureIsNotAnInvalidCredentialAndMeDoesNotNeedRedis() {
        var a = login.login("code-a", "proxy");
        clearInvocations(guard);
        doThrow(new DataAccessResourceFailureException("unavailable")).when(accounts).findSession(anyString());
        authFailure(AppError.DEPENDENCY_UNAVAILABLE, () -> sessions.authenticate(a.getAccessToken()));
        verifyNoInteractions(guard);
    }
    @Test void malformedUnknownAndForeignTokensDoNotResolveAUser() {
        authFailure(AppError.TOKEN_INVALID, () -> sessions.authenticate("admin-token"));
        authFailure(AppError.TOKEN_INVALID, () -> sessions.authenticate(AppTokens.generate()));
        authFailure(AppError.TOKEN_INVALID, () -> sessions.authenticate("app_" + "A".repeat(44)));
    }
    @Test void missingConfigCannotExchangeCodeOrCreateUsers() {
        properties.setWechatSecret("");
        authFailure(AppError.NOT_CONFIGURED, () -> login.login("code", "proxy"));
        verifyNoInteractions(wechat, guard);
        assertEquals(0, count("app_user"));
    }
    @Test void upstreamFailureCannotCreateBusinessState() {
        when(wechat.exchange(anyString())).thenThrow(new AppAuthException(AppError.WECHAT_TIMEOUT));
        authFailure(AppError.WECHAT_TIMEOUT, () -> login.login("code", "proxy"));
        assertEquals(0, count("app_user"));
        verify(guard).complete("code");
    }
}
