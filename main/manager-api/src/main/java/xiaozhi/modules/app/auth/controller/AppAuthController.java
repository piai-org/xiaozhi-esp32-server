package xiaozhi.modules.app.auth.controller;

import java.util.Map;
import java.io.IOException;
import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.core.JsonToken;
import com.fasterxml.jackson.databind.DeserializationContext;
import com.fasterxml.jackson.databind.JsonDeserializer;
import com.fasterxml.jackson.databind.annotation.JsonDeserialize;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.security.SecurityRequirement;
import io.swagger.v3.oas.annotations.tags.Tag;
import xiaozhi.common.utils.Result;
import xiaozhi.modules.app.auth.AppAuthException;
import xiaozhi.modules.app.auth.AppError;
import xiaozhi.modules.app.auth.AppLoginCoordinator;
import xiaozhi.modules.app.auth.session.AppSessionService;
import xiaozhi.modules.app.security.AppPrincipal;

@RestController
@RequestMapping("/app/v1")
@Tag(name = "小程序本人登录")
public class AppAuthController {
    private final AppLoginCoordinator login;
    private final AppSessionService sessions;
    public AppAuthController(AppLoginCoordinator login, AppSessionService sessions) {
        this.login = login;
        this.sessions = sessions;
    }
    public static final class LoginRequest {
        @NotBlank @Size(max = 512) @Pattern(regexp = "[A-Za-z0-9_-]+")
        @JsonDeserialize(using = CodeDeserializer.class)
        private String code;
        public String getCode() { return code; }
        public void setCode(String code) { this.code = code; }
    }
    public static final class CodeDeserializer extends JsonDeserializer<String> {
        @Override
        public String deserialize(JsonParser parser, DeserializationContext context) throws IOException {
            if (!parser.hasToken(JsonToken.VALUE_STRING))
                return context.reportInputMismatch(String.class, "code must be a string");
            return parser.getText();
        }
    }
    @PostMapping("/auth/wechat/login")
    @Operation(summary = "用wx.login的新code交换本人业务会话；不需要设备、头像或手机号")
    public Result<AppLoginResponse> login(@Valid @RequestBody LoginRequest body, HttpServletRequest request) {
        // Do not trust caller-supplied X-Forwarded-For for the rate-limit identity.
        return new Result<AppLoginResponse>().ok(login.login(body.getCode(), request.getRemoteAddr()));
    }
    @GetMapping("/me")
    @Operation(summary = "校验业务会话并获取当前本人", security = @SecurityRequirement(name = "miniappBearer"))
    public Result<Map<String, String>> me(HttpServletRequest request) {
        return new Result<Map<String, String>>().ok(Map.of("user_id", principal(request).userId()));
    }
    @PostMapping("/auth/logout")
    @Operation(summary = "撤销当前业务会话；不影响其他会话", security = @SecurityRequirement(name = "miniappBearer"))
    public Result<Void> logout(HttpServletRequest request) {
        sessions.logout(principal(request));
        return new Result<>();
    }
    private AppPrincipal principal(HttpServletRequest request) {
        Object principal = request.getAttribute(AppPrincipal.REQUEST_ATTRIBUTE);
        if (!(principal instanceof AppPrincipal app)) throw new AppAuthException(AppError.TOKEN_MISSING);
        return app;
    }
}
