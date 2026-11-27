package xiaozhi.modules.app.auth.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/** Server-only configuration. Deliberately has no toString containing credentials. */
@ConfigurationProperties("app.auth")
public class AppAuthProperties {
    private boolean enabled;
    private String wechatAppId = "";
    private String wechatSecret = "";
    private int sessionTtlSeconds = 604800;
    private int ipLoginsPerMinute = 120;
    private int totalLoginsPerMinute = 600;

    public boolean isEnabled() { return enabled; }
    public void setEnabled(boolean enabled) { this.enabled = enabled; }
    public String getWechatAppId() { return wechatAppId; }
    public void setWechatAppId(String value) { wechatAppId = value; }
    public String getWechatSecret() { return wechatSecret; }
    public void setWechatSecret(String value) { wechatSecret = value; }
    public int getSessionTtlSeconds() { return sessionTtlSeconds; }
    public void setSessionTtlSeconds(int value) { sessionTtlSeconds = value; }
    public int getIpLoginsPerMinute() { return ipLoginsPerMinute; }
    public void setIpLoginsPerMinute(int value) { ipLoginsPerMinute = value; }
    public int getTotalLoginsPerMinute() { return totalLoginsPerMinute; }
    public void setTotalLoginsPerMinute(int value) { totalLoginsPerMinute = value; }

    public boolean loginConfigured() {
        return enabled && wechatAppId != null && !wechatAppId.isBlank()
                && wechatSecret != null && !wechatSecret.isBlank()
                && sessionTtlSeconds >= 60 && sessionTtlSeconds <= 2592000
                && ipLoginsPerMinute > 0 && totalLoginsPerMinute > 0;
    }
}
