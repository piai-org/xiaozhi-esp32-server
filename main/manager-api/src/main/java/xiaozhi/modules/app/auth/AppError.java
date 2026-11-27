package xiaozhi.modules.app.auth;

public enum AppError {
    BAD_REQUEST(400, 61001, "请求参数不正确"),
    CODE_INVALID(400, 61002, "微信登录凭证无效，请重新登录"),
    CODE_USED(400, 61003, "微信登录凭证已处理，请重新获取"),
    CODE_PROCESSING(409, 61004, "登录正在处理中，请勿重复提交"),
    WECHAT_UNAVAILABLE(503, 61005, "微信登录暂时不可用，请稍后重新登录"),
    WECHAT_TIMEOUT(504, 61006, "微信登录超时，请重新获取登录凭证"),
    NOT_CONFIGURED(503, 61007, "微信登录尚未启用或配置不正确"),
    LOGIN_TIMEOUT(504, 61008, "登录处理超时，请重新获取微信登录凭证"),
    TOKEN_MISSING(401, 61101, "请先登录"),
    TOKEN_INVALID(401, 61102, "登录凭据无效，请重新登录"),
    TOKEN_EXPIRED(401, 61103, "登录已过期，请重新登录"),
    TOKEN_REVOKED(401, 61104, "登录已退出，请重新登录"),
    USER_DISABLED(403, 61105, "账号已停用"),
    RATE_LIMITED(429, 61901, "登录请求过于频繁，请稍后重试"),
    DEPENDENCY_UNAVAILABLE(503, 61902, "服务暂时不可用，请稍后重试"),
    INTERNAL_ERROR(500, 61999, "服务处理失败，请稍后重试");

    public final int status;
    public final int code;
    public final String message;
    AppError(int status, int code, String message) {
        this.status = status;
        this.code = code;
        this.message = message;
    }
}
