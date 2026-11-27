package xiaozhi.modules.app.auth;

import static org.junit.jupiter.api.Assertions.*;
import java.util.concurrent.*;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory;
import org.springframework.data.redis.core.StringRedisTemplate;
import xiaozhi.modules.app.auth.config.AppAuthProperties;

@EnabledIfEnvironmentVariable(named = "MINIAPP_TEST_JDBC_URL", matches = ".+")
class RedisLoginGuardIntegrationTest {
    private LettuceConnectionFactory factory;
    private StringRedisTemplate redis;
    private AppAuthProperties properties;
    private RedisLoginGuard guard;
    @BeforeEach void setup() {
        // Fixed isolated Docker alias, never the production Redis configuration.
        factory = new LettuceConnectionFactory("test-redis", 6379);
        factory.afterPropertiesSet();
        redis = new StringRedisTemplate(factory);
        try (var connection = factory.getConnection()) { connection.serverCommands().flushDb(); }
        properties = new AppAuthProperties();
        properties.setWechatAppId("test-app");
        guard = new RedisLoginGuard(redis, properties);
    }
    @AfterEach void close() { factory.destroy(); }
    @Test void processingAndProcessedAreDistinctAndNeitherCachesTokenOrRawCode() {
        guard.begin("SECRET-CODE", "127.0.0.1");
        assertEquals(AppError.CODE_PROCESSING,
                assertThrows(AppAuthException.class, () -> guard.begin("SECRET-CODE", "127.0.0.1")).getError());
        guard.complete("SECRET-CODE");
        assertEquals(AppError.CODE_USED,
                assertThrows(AppAuthException.class, () -> guard.begin("SECRET-CODE", "127.0.0.1")).getError());
        for (String key : redis.keys("miniapp:auth:v1:*")) {
            assertFalse(key.contains("SECRET-CODE"));
            assertFalse(key.contains("127.0.0.1"));
            assertTrue(redis.getExpire(key) > 0);
            assertFalse(redis.opsForValue().get(key).contains("app_"));
        }
    }
    @Test void concurrentSameCodeOnlyHasOneWinner() throws Exception {
        try (ExecutorService pool = Executors.newFixedThreadPool(2)) {
            Callable<Boolean> attempt = () -> {
                try { guard.begin("same-code", "proxy"); return true; }
                catch (AppAuthException failure) { assertEquals(AppError.CODE_PROCESSING, failure.getError()); return false; }
            };
            var outcomes = pool.invokeAll(java.util.List.of(attempt, attempt));
            assertNotEquals(outcomes.get(0).get(), outcomes.get(1).get());
        }
    }
    @Test void rateLimitIsSharedAcrossCodesAndCannotBeAvoidedByRetryingCode() {
        properties.setIpLoginsPerMinute(1);
        guard.begin("one", "proxy");
        assertEquals(AppError.RATE_LIMITED,
                assertThrows(AppAuthException.class, () -> guard.begin("two", "proxy")).getError());
        guard.begin("three", "different-proxy");
    }
    @Test void globalLimitAppliesAcrossIps() {
        properties.setTotalLoginsPerMinute(1);
        guard.begin("one", "proxy");
        assertEquals(AppError.RATE_LIMITED,
                assertThrows(AppAuthException.class, () -> guard.begin("two", "different-proxy")).getError());
    }
}
