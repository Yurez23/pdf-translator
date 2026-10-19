"""Safe diagnostic facts shared by application errors and instrumentation."""


class DiagnosticError(ValueError):
    """Expected application failure. Messages must already be safe for display."""

    diagnostic_code = "input_or_validation"
