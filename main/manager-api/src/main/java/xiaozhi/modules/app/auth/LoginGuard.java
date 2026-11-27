package xiaozhi.modules.app.auth;

public interface LoginGuard {
    void begin(String code, String remoteAddress);
    void complete(String code);
}
