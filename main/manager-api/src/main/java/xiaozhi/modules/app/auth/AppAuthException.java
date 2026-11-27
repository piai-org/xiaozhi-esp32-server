package xiaozhi.modules.app.auth;

/** Never attach an upstream exception: its URI may contain AppSecret or code. */
public class AppAuthException extends RuntimeException {
    private final AppError error;
    public AppAuthException(AppError error) {
        super(error.message);
        this.error = error;
    }
    public AppError getError() { return error; }
}
