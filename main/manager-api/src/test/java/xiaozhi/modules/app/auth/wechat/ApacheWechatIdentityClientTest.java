package xiaozhi.modules.app.auth.wechat;

import static org.junit.jupiter.api.Assertions.*;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import xiaozhi.modules.app.auth.AppAuthException;
import xiaozhi.modules.app.auth.AppError;
import xiaozhi.modules.app.auth.config.AppAuthProperties;

class ApacheWechatIdentityClientTest {
    private HttpServer server;
    private ApacheWechatIdentityClient client;
    private volatile String body;
    private volatile int status;
    private volatile long bodyDelay;
    private volatile boolean closeWithoutResponse;
    private final AtomicInteger calls = new AtomicInteger();

    @BeforeEach
    void setup() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
        status = 200;
        body = "{\"openid\":\"verified-open-id\",\"session_key\":\"DO-NOT-RETURN\",\"unionid\":\"ignored\"}";
        server.createContext("/exchange", exchange -> {
            calls.incrementAndGet();
            if (closeWithoutResponse) { exchange.close(); return; }
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(status, bytes.length);
            try {
                Thread.sleep(bodyDelay);
                exchange.getResponseBody().write(bytes);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            } finally { exchange.close(); }
        });
        server.start();
        AppAuthProperties properties = new AppAuthProperties();
        properties.setEnabled(true);
        properties.setWechatAppId("test-app");
        properties.setWechatSecret("FAKE-SECRET-NOT-LOGGED");
        client = new ApacheWechatIdentityClient(properties,
                URI.create("http://127.0.0.1:" + server.getAddress().getPort() + "/exchange"), Duration.ofMillis(500));
    }
    @AfterEach void close() { client.close(); server.stop(0); }

    @Test void returnsOnlyVerifiedOpenId() { assertEquals("verified-open-id", client.exchange("test-code")); }

    @ParameterizedTest
    @CsvSource({"40029,CODE_INVALID", "40163,CODE_USED", "40013,NOT_CONFIGURED", "40125,NOT_CONFIGURED",
            "-1,WECHAT_UNAVAILABLE", "45011,WECHAT_UNAVAILABLE", "999999,WECHAT_UNAVAILABLE"})
    void mapsWechatErrorsWithoutLeakingRemoteDetails(int code, AppError expected) {
        body = "{\"errcode\":" + code + ",\"errmsg\":\"FAKE-SECRET-NOT-LOGGED code=test-code\"}";
        AppAuthException failure = assertThrows(AppAuthException.class, () -> client.exchange("test-code"));
        assertEquals(expected, failure.getError());
        assertNull(failure.getCause());
        assertFalse(failure.toString().contains("FAKE-SECRET"));
        assertFalse(failure.toString().contains("test-code"));
        assertEquals(1, calls.get());
    }
    @ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(strings = {
            "{}", "null", "[]", "not-json", "{\"openid\":\"\"}", "{\"openid\":123}",
            "{\"openid\":\"x\",\"errcode\":\"0\"}", "{\"openid\":\"x\",\"errcode\":null}"})
    void rejectsMalformedSuccess(String response) {
        body = response;
        assertEquals(AppError.WECHAT_UNAVAILABLE,
                assertThrows(AppAuthException.class, () -> client.exchange("test-code")).getError());
    }
    @Test void httpErrorDoesNotCreateIdentityOrFollowRedirects() {
        status = 302;
        assertEquals(AppError.WECHAT_UNAVAILABLE,
                assertThrows(AppAuthException.class, () -> client.exchange("test-code")).getError());
        assertEquals(1, calls.get());
    }
    @Test void deadlineIncludesSlowBodyAndNeverRetriesCode() {
        bodyDelay = 1500;
        long start = System.nanoTime();
        assertEquals(AppError.WECHAT_TIMEOUT,
                assertThrows(AppAuthException.class, () -> client.exchange("test-code")).getError());
        assertTrue(Duration.ofNanos(System.nanoTime() - start).toMillis() < 1400);
        assertEquals(1, calls.get());
    }
    @Test void connectionClosedWithoutResponseMustNotResubmitOneUseCode() {
        closeWithoutResponse = true;
        AppError outcome = assertThrows(AppAuthException.class, () -> client.exchange("test-code")).getError();
        assertTrue(outcome == AppError.WECHAT_UNAVAILABLE || outcome == AppError.WECHAT_TIMEOUT);
        assertEquals(1, calls.get(), "The one-use code must not be retried by the transport");
    }
}
