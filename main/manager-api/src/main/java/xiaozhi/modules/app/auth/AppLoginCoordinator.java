package xiaozhi.modules.app.auth;

import java.time.Duration;
import java.util.concurrent.*;
import jakarta.annotation.PreDestroy;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import xiaozhi.modules.app.auth.controller.AppLoginResponse;

/** Bounds login resource usage and the HTTP wait, without changing management DB/Redis timeouts. */
@Service
public class AppLoginCoordinator implements AutoCloseable {
    private final AppLoginService login;
    private final Duration timeout;
    private final ThreadPoolExecutor workers;
    @Autowired
    public AppLoginCoordinator(AppLoginService login) { this(login, Duration.ofSeconds(12), 4); }
    AppLoginCoordinator(AppLoginService login, Duration timeout, int concurrency) {
        this.login = login;
        this.timeout = timeout;
        workers = new ThreadPoolExecutor(0, concurrency, 30, TimeUnit.SECONDS, new SynchronousQueue<>(),
                Thread.ofPlatform().daemon(true).name("miniapp-login-", 0).factory(), new ThreadPoolExecutor.AbortPolicy());
    }
    public AppLoginResponse login(String code, String remoteAddress) {
        Future<AppLoginResponse> pending;
        try {
            // No queue: an overloaded login endpoint cannot fill Tomcat/DB with pending work.
            pending = workers.submit(() -> login.login(code, remoteAddress));
        } catch (RejectedExecutionException overloaded) {
            throw new AppAuthException(AppError.RATE_LIMITED);
        }
        try {
            return pending.get(timeout.toMillis(), TimeUnit.MILLISECONDS);
        } catch (TimeoutException timedOut) {
            pending.cancel(true);
            // A transaction might already have committed. The caller must use a NEW WeChat code.
            throw new AppAuthException(AppError.LOGIN_TIMEOUT);
        } catch (InterruptedException interrupted) {
            pending.cancel(true);
            Thread.currentThread().interrupt();
            throw new AppAuthException(AppError.DEPENDENCY_UNAVAILABLE);
        } catch (ExecutionException failure) {
            if (failure.getCause() instanceof AppAuthException safe) throw safe;
            // Do not retain a potentially sensitive exception/cause from external or SQL adapters.
            throw new AppAuthException(AppError.INTERNAL_ERROR);
        }
    }
    @Override @PreDestroy public void close() { workers.shutdownNow(); }
}
