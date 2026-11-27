package xiaozhi.modules.app.auth;

import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.UUID;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.dao.DataAccessException;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;
import xiaozhi.modules.app.account.AppAccountRepository;
import xiaozhi.modules.app.auth.config.AppAuthProperties;
import xiaozhi.modules.app.auth.controller.AppLoginResponse;
import xiaozhi.modules.app.auth.session.AppTokens;
import xiaozhi.modules.app.auth.wechat.WechatIdentityClient;

@Service
public class AppLoginService {
    private final AppAuthProperties properties;
    private final AppAccountRepository accounts;
    private final WechatIdentityClient wechat;
    private final LoginGuard guard;
    private final Clock clock;
    private final TransactionTemplate transaction;

    public AppLoginService(AppAuthProperties properties, AppAccountRepository accounts, WechatIdentityClient wechat,
            LoginGuard guard, @Qualifier("appAuthClock") Clock clock, PlatformTransactionManager transactionManager) {
        this.properties = properties;
        this.accounts = accounts;
        this.wechat = wechat;
        this.guard = guard;
        this.clock = clock;
        transaction = new TransactionTemplate(transactionManager);
        transaction.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
        transaction.setTimeout(3);
    }

    public AppLoginResponse login(String code, String remoteAddress) {
        if (!properties.loginConfigured()) throw new AppAuthException(AppError.NOT_CONFIGURED);
        if (code == null || !code.matches("[A-Za-z0-9_-]{1,512}")) throw new AppAuthException(AppError.BAD_REQUEST);
        guard.begin(code, remoteAddress);
        try {
            // WeChat's single-use code is exchanged exactly once, outside the database transaction.
            String openId = wechat.exchange(code);
            for (int attempt = 0; attempt < 2; attempt++) {
                try {
                    return transaction.execute(status -> persist(openId));
                } catch (DuplicateKeyException conflict) {
                    // The losing first-login transaction has fully rolled back (including its app_user).
                    // Re-read the winner in a NEW transaction, never in the old repeatable-read snapshot.
                    if (attempt == 1) throw new AppAuthException(AppError.DEPENDENCY_UNAVAILABLE);
                }
            }
            throw new AppAuthException(AppError.DEPENDENCY_UNAVAILABLE);
        } catch (DataAccessException failure) {
            throw new AppAuthException(AppError.DEPENDENCY_UNAVAILABLE);
        } catch (org.springframework.transaction.TransactionException failure) {
            throw new AppAuthException(AppError.DEPENDENCY_UNAVAILABLE);
        } finally {
            guard.complete(code);
        }
    }

    private AppLoginResponse persist(String openId) {
        Instant now = clock.instant().truncatedTo(ChronoUnit.MILLIS);
        AppAccountRepository.User user = accounts.findIdentity(properties.getWechatAppId(), openId);
        if (user == null) {
            String id = "u_" + UUID.randomUUID();
            accounts.createUser(id, now);
            accounts.createIdentity(UUID.randomUUID().toString(), id, properties.getWechatAppId(), openId, now);
            user = new AppAccountRepository.User(id, 1);
        } else {
            user = accounts.lockUser(user.id());
        }
        if (user.status() != 1) throw new AppAuthException(AppError.USER_DISABLED);
        String token = AppTokens.generate();
        Instant expires = now.plusSeconds(properties.getSessionTtlSeconds());
        accounts.createSession(UUID.randomUUID().toString(), user.id(), AppTokens.digest(token), now, expires);
        return new AppLoginResponse(user.id(), token, properties.getSessionTtlSeconds(), expires.toString());
    }
}
