package xiaozhi.modules.app.auth.controller;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.ConstraintViolationException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import xiaozhi.common.utils.Result;
import xiaozhi.modules.app.auth.AppAuthException;
import xiaozhi.modules.app.auth.AppError;

/** Scope and priority preserve the legacy management APIs' error contract. */
@Order(Ordered.HIGHEST_PRECEDENCE)
@RestControllerAdvice(basePackageClasses = AppAuthController.class)
public class AppExceptionAdvice {
    private static final Logger LOG = LoggerFactory.getLogger(AppExceptionAdvice.class);
    @ExceptionHandler(AppAuthException.class)
    public ResponseEntity<Result<Void>> auth(AppAuthException failure, HttpServletRequest request) {
        request.setAttribute("appOutcome", failure.getError().code);
        return result(failure.getError());
    }
    @ExceptionHandler({MethodArgumentNotValidException.class, HttpMessageNotReadableException.class,
            ConstraintViolationException.class})
    public ResponseEntity<Result<Void>> invalid(Exception failure, HttpServletRequest request) {
        request.setAttribute("appOutcome", AppError.BAD_REQUEST.code);
        return result(AppError.BAD_REQUEST);
    }
    @ExceptionHandler(Exception.class)
    public ResponseEntity<Result<Void>> unexpected(Exception failure, HttpServletRequest request) {
        // Never log exception text, raw body, upstream URI or throwable cause chain.
        LOG.error("miniapp unexpected error request_id={} type={}", request.getAttribute("appRequestId"),
                failure.getClass().getSimpleName());
        request.setAttribute("appOutcome", AppError.INTERNAL_ERROR.code);
        return result(AppError.INTERNAL_ERROR);
    }
    private ResponseEntity<Result<Void>> result(AppError error) {
        var response = ResponseEntity.status(error.status).header("Cache-Control", "no-store");
        if (error == AppError.RATE_LIMITED) response.header("Retry-After", "60");
        return response.body(new Result<Void>().error(error.code, error.message));
    }
}
