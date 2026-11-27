package xiaozhi.modules.app.auth.session;

import java.time.Clock;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.dao.DataAccessException;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import xiaozhi.modules.app.account.AppAccountRepository;
import xiaozhi.modules.app.auth.AppAuthException;
import xiaozhi.modules.app.auth.AppError;
import xiaozhi.modules.app.auth.config.AppAuthProperties;
import xiaozhi.modules.app.security.AppPrincipal;

@Service
public class AppSessionService {
    private static final Logger LOG = LoggerFactory.getLogger(AppSessionService.class);
    private final AppAccountRepository accounts;
    private final AppAuthProperties properties;
    private final Clock clock;
    public AppSessionService(AppAccountRepository accounts, AppAuthProperties properties,
            @Qualifier("appAuthClock") Clock clock) {
        this.accounts = accounts;
        this.properties = properties;
        this.clock = clock;
    }
    public void requireEnabled() {
        if (!properties.isEnabled()) throw new AppAuthException(AppError.NOT_CONFIGURED);
    }
    public AppPrincipal authenticate(String token) {
        requireEnabled();
        if (!AppTokens.validFormat(token)) throw new AppAuthException(AppError.TOKEN_INVALID);
        try {
            var session = accounts.findSession(AppTokens.digest(token));
            if (session == null) throw new AppAuthException(AppError.TOKEN_INVALID);
            if (session.userStatus() != 1) throw new AppAuthException(AppError.USER_DISABLED);
            if (session.revokedAt() != null) throw new AppAuthException(AppError.TOKEN_REVOKED);
            if (!clock.instant().isBefore(session.expiresAt())) throw new AppAuthException(AppError.TOKEN_EXPIRED);
            return new AppPrincipal(session.userId(), session.id());
        } catch (DataAccessException failure) {
            throw new AppAuthException(AppError.DEPENDENCY_UNAVAILABLE);
        }
    }
    public void logout(AppPrincipal principal) {
        try {
            accounts.revoke(principal.sessionId(), principal.userId(), clock.instant());
        } catch (DataAccessException failure) {
            throw new AppAuthException(AppError.DEPENDENCY_UNAVAILABLE);
        }
    }
    @Scheduled(fixedDelay = 3600000, initialDelay = 3600000)
    public void cleanup() {
        if (!properties.isEnabled()) return;
        try {
            // Retain invalid-session diagnostics seven days; bound each run's database work.
            accounts.cleanup(clock.instant().minusSeconds(7 * 86400L));
        } catch (DataAccessException failure) {
            LOG.warn("miniapp session cleanup unavailable");
        }
    }
}
