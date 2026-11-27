package xiaozhi.modules.app.auth.wechat;

public interface WechatIdentityClient {
    /** Returns the verified OpenID only; session_key is deliberately discarded. */
    String exchange(String code);
}
