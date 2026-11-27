package xiaozhi.modules.app.auth;

import java.time.Duration;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Component;
import xiaozhi.modules.app.auth.config.AppAuthProperties;
import xiaozhi.modules.app.auth.session.AppTokens;

@Component
public class RedisLoginGuard implements LoginGuard {
    private static final Logger LOG = LoggerFactory.getLogger(RedisLoginGuard.class);
    private static final Duration CODE_TTL = Duration.ofMinutes(6);
    private static final String PREFIX = "miniapp:auth:v1:";
    private static final DefaultRedisScript<Long> COUNT = new DefaultRedisScript<>(
            "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n", Long.class);
    private final StringRedisTemplate redis;
    private final AppAuthProperties properties;
    public RedisLoginGuard(StringRedisTemplate redis, AppAuthProperties properties) {
        this.redis = redis;
        this.properties = properties;
    }
    @Override
    public void begin(String code, String remoteAddress) {
        try {
            Long total = redis.execute(COUNT, List.of(PREFIX + "rate:all"));
            Long ip = redis.execute(COUNT, List.of(PREFIX + "rate:ip:" + AppTokens.digest(remoteAddress)));
            if (total == null || ip == null) throw new AppAuthException(AppError.DEPENDENCY_UNAVAILABLE);
            if (total > properties.getTotalLoginsPerMinute() || ip > properties.getIpLoginsPerMinute())
                throw new AppAuthException(AppError.RATE_LIMITED);
            Boolean acquired = redis.opsForValue().setIfAbsent(key(code), "processing", CODE_TTL);
            if (acquired == null) throw new AppAuthException(AppError.DEPENDENCY_UNAVAILABLE);
            if (!acquired) {
                String state = redis.opsForValue().get(key(code));
                throw new AppAuthException("processing".equals(state) ? AppError.CODE_PROCESSING : AppError.CODE_USED);
            }
        } catch (DataAccessException failure) {
            throw new AppAuthException(AppError.DEPENDENCY_UNAVAILABLE);
        }
    }
    @Override
    public void complete(String code) {
        try {
            redis.opsForValue().set(key(code), "processed", CODE_TTL);
        } catch (DataAccessException failure) {
            // The original short processing lock still prevents immediate replay. Never mask a committed login.
            LOG.warn("miniapp login guard completion unavailable");
        }
    }
    private String key(String code) {
        return PREFIX + "code:" + AppTokens.digest(properties.getWechatAppId() + "\n" + code);
    }
}
