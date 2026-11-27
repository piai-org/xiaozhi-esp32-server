package xiaozhi.modules.app.auth;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import java.time.Duration;
import java.util.concurrent.*;
import org.junit.jupiter.api.Test;

class AppLoginCoordinatorTest {
    @Test void totalDeadlineIsBoundedAndWorkerIsInterrupted() throws Exception {
        AppLoginService service = mock(AppLoginService.class);
        CountDownLatch interrupted = new CountDownLatch(1);
        when(service.login("code", "proxy")).thenAnswer(invocation -> {
            try { Thread.sleep(5000); return null; }
            catch (InterruptedException stop) { interrupted.countDown(); throw stop; }
        });
        try (var coordinator = new AppLoginCoordinator(service, Duration.ofMillis(100), 1)) {
            var failure = assertThrows(AppAuthException.class, () -> coordinator.login("code", "proxy"));
            assertEquals(AppError.LOGIN_TIMEOUT, failure.getError());
            assertTrue(interrupted.await(1, TimeUnit.SECONDS));
        }
    }
    @Test void overloadDoesNotQueueOrConsumeAnotherCode() throws Exception {
        AppLoginService service = mock(AppLoginService.class);
        CountDownLatch started = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        when(service.login("one", "proxy")).thenAnswer(invocation -> { started.countDown(); release.await(); return null; });
        try (var coordinator = new AppLoginCoordinator(service, Duration.ofSeconds(3), 1);
                var caller = Executors.newSingleThreadExecutor()) {
            var first = caller.submit(() -> coordinator.login("one", "proxy"));
            assertTrue(started.await(1, TimeUnit.SECONDS));
            assertEquals(AppError.RATE_LIMITED,
                    assertThrows(AppAuthException.class, () -> coordinator.login("two", "proxy")).getError());
            verify(service, never()).login("two", "proxy");
            release.countDown();
            first.get(1, TimeUnit.SECONDS);
        }
    }
    @Test void unknownExceptionDoesNotRetainSecretCause() {
        AppLoginService service = mock(AppLoginService.class);
        when(service.login("code", "proxy")).thenThrow(new IllegalStateException("secret-url"));
        try (var coordinator = new AppLoginCoordinator(service, Duration.ofSeconds(1), 1)) {
            var failure = assertThrows(AppAuthException.class, () -> coordinator.login("code", "proxy"));
            assertEquals(AppError.INTERNAL_ERROR, failure.getError());
            assertNull(failure.getCause());
        }
    }
}
