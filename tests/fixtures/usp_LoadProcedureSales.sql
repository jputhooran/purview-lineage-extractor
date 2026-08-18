CREATE PROCEDURE dbo.usp_LoadProcedureSales
AS
BEGIN
    SET NOCOUNT ON;

    DELETE FROM dbo.ProcedureSales;

    INSERT dbo.ProcedureSales
        (OrderID, CustomerNameUpper, RegionCode, RegionName, TotalAmount, OrderMonth)
    SELECT
        o.OrderID,
        UPPER(o.CustomerName),
        o.RegionCode,
        r.RegionName,
        CAST(o.Quantity * o.UnitPrice AS DECIMAL(18, 2)),
        DATEFROMPARTS(YEAR(o.OrderDate), MONTH(o.OrderDate), 1)
    FROM SPLineageSource.dbo.ProcedureOrders AS o
    INNER JOIN SPLineageSource.dbo.ProcedureRegions AS r
        ON r.RegionCode = o.RegionCode;
END;
