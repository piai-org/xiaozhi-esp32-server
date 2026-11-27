package xiaozhi.modules.app.auth.wechat;

import java.net.URI;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.Future;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import jakarta.annotation.PreDestroy;
import org.apache.hc.client5.http.async.methods.SimpleHttpResponse;
import org.apache.hc.client5.http.async.methods.SimpleRequestBuilder;
import org.apache.hc.client5.http.config.ConnectionConfig;
import org.apache.hc.client5.http.config.RequestConfig;
import org.apache.hc.client5.http.impl.async.CloseableHttpAsyncClient;
import org.apache.hc.client5.http.impl.async.HttpAsyncClients;
import org.apache.hc.client5.http.impl.nio.PoolingAsyncClientConnectionManagerBuilder;
import org.apache.hc.core5.io.CloseMode;
import org.apache.hc.core5.reactor.IOReactorConfig;
import org.apache.hc.core5.util.Timeout;
import org.springframework.stereotype.Component;
import org.springframework.beans.factory.annotation.Autowired;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import xiaozhi.modules.app.auth.AppAuthException;
import xiaozhi.modules.app.auth.AppError;
import xiaozhi.modules.app.auth.config.AppAuthProperties;

@Component
public class ApacheWechatIdentityClient implements WechatIdentityClient, AutoCloseable {
    private static final URI ENDPOINT = URI.create("https://api.weixin.qq.com/sns/jscode2session");
    private final AppAuthProperties properties;
    private final CloseableHttpAsyncClient http;
    private final URI endpoint;
    private final Duration timeout;
    private final ObjectMapper json = new ObjectMapper();

    @Autowired
    public ApacheWechatIdentityClient(AppAuthProperties properties) {
        this(properties, ENDPOINT, Duration.ofSeconds(6));
    }
    // Only package-visible for adapter tests. No production/mock endpoint property.
    ApacheWechatIdentityClient(AppAuthProperties properties, URI endpoint, Duration timeout) {
        this.properties = properties;
        this.endpoint = endpoint;
        this.timeout = timeout;
        var connections = PoolingAsyncClientConnectionManagerBuilder.create()
                .setMaxConnTotal(4).setMaxConnPerRoute(4)
                .setDefaultConnectionConfig(ConnectionConfig.custom()
                        .setConnectTimeout(Timeout.ofSeconds(2)).setSocketTimeout(Timeout.ofSeconds(3)).build()).build();
        this.http = HttpAsyncClients.custom().setConnectionManager(connections)
                .setIOReactorConfig(IOReactorConfig.custom().setIoThreadCount(1).build())
                .setDefaultRequestConfig(RequestConfig.custom().setConnectionRequestTimeout(Timeout.ofSeconds(1))
                        .setResponseTimeout(Timeout.ofSeconds(3)).build())
                // Unlike a regular GET, code2Session consumes a one-use credential. Disable transport retries too.
                .disableAutomaticRetries().disableRedirectHandling().disableCookieManagement().build();
    }
    @Override
    public String exchange(String code) {
        if (!properties.loginConfigured()) throw new AppAuthException(AppError.NOT_CONFIGURED);
        URI uri = URI.create(endpoint + "?appid=" + encode(properties.getWechatAppId())
                + "&secret=" + encode(properties.getWechatSecret()) + "&js_code=" + encode(code)
                + "&grant_type=authorization_code");
        Future<SimpleHttpResponse> pending = null;
        try {
            http.start(); // No reactor thread/network activity while the feature is disabled or unconfigured.
            pending = http.execute(SimpleRequestBuilder.get(uri).build(), null);
            // Covers connection acquisition, exchange and reading the response body.
            SimpleHttpResponse response = pending.get(timeout.toMillis(), TimeUnit.MILLISECONDS);
            String text = response.getBodyText();
            if (response.getCode() != 200 || text == null || text.length() > 16384)
                throw new AppAuthException(AppError.WECHAT_UNAVAILABLE);
            JsonNode body = json.readTree(text);
            if (body == null || !body.isObject()) throw new AppAuthException(AppError.WECHAT_UNAVAILABLE);
            JsonNode error = body.get("errcode");
            if (error != null && (!error.isIntegralNumber() || !error.canConvertToInt()))
                throw new AppAuthException(AppError.WECHAT_UNAVAILABLE);
            int errorCode = error == null ? 0 : error.intValue();
            if (errorCode != 0) throw new AppAuthException(switch (errorCode) {
                case 40029 -> AppError.CODE_INVALID;
                case 40163 -> AppError.CODE_USED;
                case 40013, 40125, 40001 -> AppError.NOT_CONFIGURED;
                default -> AppError.WECHAT_UNAVAILABLE;
            });
            JsonNode openId = body.get("openid");
            if (openId == null || !openId.isTextual() || !openId.textValue().matches("[A-Za-z0-9_-]{1,128}"))
                throw new AppAuthException(AppError.WECHAT_UNAVAILABLE);
            return openId.textValue();
        } catch (AppAuthException safe) {
            throw safe;
        } catch (TimeoutException timedOut) {
            throw new AppAuthException(AppError.WECHAT_TIMEOUT);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw new AppAuthException(AppError.WECHAT_UNAVAILABLE);
        } catch (ExecutionException failure) {
            if (failure.getCause() instanceof java.io.InterruptedIOException)
                throw new AppAuthException(AppError.WECHAT_TIMEOUT);
            throw new AppAuthException(AppError.WECHAT_UNAVAILABLE);
        } catch (Exception failure) {
            throw new AppAuthException(AppError.WECHAT_UNAVAILABLE);
        } finally {
            if (pending != null && !pending.isDone()) pending.cancel(true);
        }
    }
    private static String encode(String value) { return URLEncoder.encode(value, StandardCharsets.UTF_8); }
    @Override @PreDestroy public void close() { http.close(CloseMode.IMMEDIATE); }
}
