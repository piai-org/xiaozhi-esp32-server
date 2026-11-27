package xiaozhi.modules.app.security;

import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.apache.shiro.web.filter.PathMatchingFilter;
import jakarta.servlet.ServletRequest;
import jakarta.servlet.ServletResponse;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import com.fasterxml.jackson.databind.ObjectMapper;
import xiaozhi.common.utils.Result;
import xiaozhi.modules.app.auth.AppAuthException;
import xiaozhi.modules.app.auth.AppError;
import xiaozhi.modules.app.auth.session.AppSessionService;

public class AppSessionFilter extends PathMatchingFilter {
    private static final Logger LOG = LoggerFactory.getLogger(AppSessionFilter.class);
    private final AppSessionService sessions;
    private final ObjectMapper json = new ObjectMapper();
    public AppSessionFilter(AppSessionService sessions) { this.sessions = sessions; }

    @Override
    protected boolean onPreHandle(ServletRequest request, ServletResponse response, Object mappedValue) throws Exception {
        HttpServletRequest req = (HttpServletRequest) request;
        HttpServletResponse res = (HttpServletResponse) response;
        String requestId = UUID.randomUUID().toString();
        req.setAttribute("appRequestId", requestId);
        req.setAttribute("appRequestStart", System.nanoTime());
        req.removeAttribute(AppPrincipal.REQUEST_ATTRIBUTE);
        res.setHeader("X-Request-ID", requestId);
        res.setHeader("Cache-Control", "no-store");
        try {
            sessions.requireEnabled();
            String path = getPathWithinApplication(req);
            if ("POST".equals(req.getMethod()) && "/app/v1/auth/wechat/login".equals(path)) return true;
            if ("OPTIONS".equals(req.getMethod())) return true;
            String header = req.getHeader("Authorization");
            if (header == null || header.isBlank()) throw new AppAuthException(AppError.TOKEN_MISSING);
            if (!header.regionMatches(true, 0, "Bearer ", 0, 7)) throw new AppAuthException(AppError.TOKEN_INVALID);
            AppPrincipal principal = sessions.authenticate(header.substring(7));
            req.setAttribute(AppPrincipal.REQUEST_ATTRIBUTE, principal);
            return true;
        } catch (AppAuthException failure) {
            AppError error = failure.getError();
            req.setAttribute("appOutcome", error.code);
            res.setStatus(error.status);
            res.setContentType("application/json;charset=UTF-8");
            json.writeValue(res.getWriter(), new Result<Void>().error(error.code, error.message));
            return false;
        }
    }

    @Override
    public void afterCompletion(ServletRequest request, ServletResponse response, Exception exception) {
        Object start = request.getAttribute("appRequestStart");
        if (start instanceof Long nanos) {
            LOG.info("miniapp request request_id={} method={} status={} result={} elapsed_ms={}",
                    request.getAttribute("appRequestId"), ((HttpServletRequest) request).getMethod(),
                    ((HttpServletResponse) response).getStatus(),
                    request.getAttribute("appOutcome") == null ? 0 : request.getAttribute("appOutcome"),
                    (System.nanoTime() - nanos) / 1000000);
        }
    }
}
