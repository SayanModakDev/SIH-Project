# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from typing import List, Optional
from app.database.connection import get_db
from app.database import models, schemas
from app.reports.pdf_report import generate_inspection_pdf
from app.rules.rule_engine import evaluate_rules, build_inspection_findings
from app.rules.applicability import get_applicable_rules

from app.core.constants import InspectionStatus, normalize_status

router = APIRouter()

@router.get("/history", response_model=List[schemas.InspectionSummary])
def get_history(
    skip: int = 0, 
    limit: int = 100,
    status: Optional[str] = None,
    category: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Get list of past inspections."""
    query = db.query(models.Inspection).order_by(desc(models.Inspection.created_at))
    
    if status:
        norm = normalize_status(status)
        if norm == InspectionStatus.NON_COMPLIANT:
            query = query.filter(models.Inspection.overall_result.in_(["NON_COMPLIANT", "NON-COMPLIANT"]))
        elif norm == InspectionStatus.NOT_VERIFIABLE:
            query = query.filter(models.Inspection.overall_result.in_(["NOT_VERIFIABLE", "NEEDS_REVIEW", "NOT-VERIFIABLE", "NEEDS-REVIEW"]))
        elif norm == InspectionStatus.COMPLIANT:
            query = query.filter(models.Inspection.overall_result == "COMPLIANT")
        elif norm == InspectionStatus.NOT_APPLICABLE:
            query = query.filter(models.Inspection.overall_result.in_(["NOT_APPLICABLE", "NOT-APPLICABLE"]))
        else:
            query = query.filter(models.Inspection.overall_result == status)
    if category:
        query = query.filter(models.Inspection.category == category)
        
    inspections = query.offset(skip).limit(limit).all()
    
    return [
        schemas.InspectionSummary(
            id=i.id,
            inspection_date=i.created_at,
            product_name=i.product_name,
            category=i.category,
            package_type=i.package_type or "RETAIL",
            import_status=i.import_status or "DOMESTIC",
            overall_result=str(normalize_status(i.overall_result) or i.overall_result or ""),
            priority=i.priority,
            inspector_name=i.inspector_name,
            created_at=i.created_at,
            report=(
                {"id": i.report.id, "file_name": i.report.file_name}
                if i.report else None
            ),
        ) for i in inspections
    ]


@router.get("/dashboard", response_model=schemas.DashboardStats)
def get_dashboard(db: Session = Depends(get_db)):
    """Get statistics for the dashboard using canonical states."""
    total = db.query(models.Inspection).count()
    compliant = db.query(models.Inspection).filter(models.Inspection.overall_result == "COMPLIANT").count()
    non_compliant = db.query(models.Inspection).filter(models.Inspection.overall_result.in_(["NON_COMPLIANT", "NON-COMPLIANT"])).count()
    not_verifiable = db.query(models.Inspection).filter(models.Inspection.overall_result.in_(["NOT_VERIFIABLE", "NEEDS_REVIEW", "NOT-VERIFIABLE", "NEEDS-REVIEW"])).count()
    not_applicable = db.query(models.Inspection).filter(models.Inspection.overall_result.in_(["NOT_APPLICABLE", "NOT-APPLICABLE"])).count()
    
    food = db.query(models.Inspection).filter(models.Inspection.category == "FOOD").count()
    cosmetic = db.query(models.Inspection).filter(models.Inspection.category == "COSMETIC").count()
    
    recent = db.query(models.Inspection).order_by(desc(models.Inspection.created_at)).limit(5).all()
    
    # Common failed parameters
    failed_rules = db.query(
        models.RuleResult.parameter, func.count(models.RuleResult.id).label('count')
    ).filter(models.RuleResult.status == "FAIL")\
     .group_by(models.RuleResult.parameter)\
     .order_by(desc('count'))\
     .limit(5).all()
     
    common_failed = [{"parameter": r[0], "count": r[1]} for r in failed_rules]
    
    recent_inspections = [
        {
            "id": i.id,
            "product_name": i.product_name,
            "category": i.category,
            "package_type": i.package_type or "RETAIL",
            "import_status": i.import_status or "DOMESTIC",
            "result": str(normalize_status(i.overall_result) or i.overall_result or ""),
            "date": i.created_at.isoformat() if i.created_at else None
        } for i in recent
    ]

    return schemas.DashboardStats(
        total_inspections=total,
        compliant=compliant,
        non_compliant=non_compliant,
        not_verifiable=not_verifiable,
        not_applicable=not_applicable,
        food_inspections=food,
        cosmetic_inspections=cosmetic,
        recent_inspections=recent_inspections,
        common_failed_parameters=common_failed
    )


@router.get("/inspection/{inspection_id}")
def get_inspection_detail(inspection_id: int, db: Session = Depends(get_db)):
    """Get full details of a specific inspection."""
    inspection = db.query(models.Inspection).filter(models.Inspection.id == inspection_id).first()
    if not inspection:
        raise HTTPException(status_code=404, detail="Inspection not found")
        
    # Serialize data
    product = {c.name: getattr(inspection.product, c.name) for c in inspection.product.__table__.columns} if inspection.product else None
    ocr_result = {c.name: getattr(inspection.ocr_result, c.name) for c in inspection.ocr_result.__table__.columns} if inspection.ocr_result else None
    extracted_fields = [{c.name: getattr(f, c.name) for c in f.__table__.columns} for f in inspection.extracted_fields]
    rule_results = []
    for r in inspection.rule_results:
        item = {c.name: getattr(r, c.name) for c in r.__table__.columns}
        ed = item.get("evidence_data") or {}
        if "binary" in ed and ed["binary"] is not None:
            item["binary"] = ed["binary"]
        else:
            if item.get("status") == "PASS":
                item["binary"] = 1
            elif item.get("status") == "FAIL":
                item["binary"] = 0
            else:
                item["binary"] = None
        item["reason"] = ed.get("reason") or item.get("message")
        if "validation_method" in ed:
            item["validation_method"] = ed["validation_method"]
        for field in ["value", "unit", "quantity_present", "unit_present", "quantity_unit_valid", "raw_value", "validation_result"]:
            if field in ed and ed[field] is not None:
                item[field] = ed[field]
        rule_results.append(item)
    images = [
        {
            "id": image.id,
            "image_index": image.image_index,
            "file_name": image.file_name,
            "image_path": f"/uploads/{image.file_name}",
            "processed_file_name": image.processed_file_name,
            "source": image.source,
        }
        for image in inspection.images
    ]
    evidence = [{c.name: getattr(e, c.name) for c in e.__table__.columns} for e in inspection.evidence_items]
    report = {c.name: getattr(inspection.report, c.name) for c in inspection.report.__table__.columns} if inspection.report else None
    
    # Build complete dict since response_model requires handling nested objects correctly.
    # Alternatively return a dict that matches the schema
    return {
        "id": inspection.id,
        "inspection_date": inspection.created_at,
        "product_name": inspection.product_name,
        "brand": inspection.brand,
        "product_type": inspection.product_type,
        "category": inspection.category,
        "category_confidence": inspection.category_confidence,
        "package_type": inspection.package_type,
        "import_status": inspection.import_status,
        "quantity_type": inspection.quantity_type,
        "overall_result": str(normalize_status(inspection.overall_result) or inspection.overall_result or ""),
        "priority": inspection.priority,
        "image_path": f"/uploads/{inspection.image_path}" if inspection.image_path else None,
        "inspector_name": inspection.inspector_name,
        "notes": inspection.notes,
        "created_at": inspection.created_at,
        "product": product,
        "images": images,
        "ocr_result": ocr_result,
        "extracted_fields": extracted_fields,
        "rule_results": rule_results,
        "evidence": evidence,
        "report": report,
        "findings": build_inspection_findings(rule_results)
    }


@router.post("/manual-input", response_model=schemas.MessageResponse)
def add_manual_input(input_data: schemas.ManualInputRequest, db: Session = Depends(get_db)):
    """Add manual measurement data and re-evaluate rules."""
    inspection = db.query(models.Inspection).filter(models.Inspection.id == input_data.inspection_id).first()
    if not inspection:
        raise HTTPException(status_code=404, detail="Inspection not found")
        
    product = inspection.product
    if product:
        product.actual_measured_weight = input_data.actual_measured_weight
        product.actual_weight_unit = input_data.actual_weight_unit
        product.measurement_source = input_data.measurement_source
        product.measurement_timestamp = func.now()
        
    if input_data.inspector_notes:
        inspection.notes = (inspection.notes or "") + "\n" + input_data.inspector_notes

    for field_name, raw_value in input_data.field_overrides.items():
        value = str(raw_value or '').strip()
        if not value:
            continue
        field = next((item for item in inspection.extracted_fields if item.field_name == field_name), None)
        if field:
            # Preserve the previous OCR value as evidence before applying the
            # inspector's correction.
            db.add(models.Evidence(inspection_id=inspection.id, parameter=field_name, evidence_type='OCR_SUPERSEDED', text_content=field.field_value, bbox=field.bbox, confidence=field.confidence))
            field.field_value, field.confidence, field.source = value, 1.0, 'MANUAL'
        else:
            db.add(models.ExtractedField(inspection_id=inspection.id, field_name=field_name, field_value=value, confidence=1.0, source='MANUAL'))
        db.add(models.Evidence(inspection_id=inspection.id, parameter=field_name, evidence_type='MANUAL_OVERRIDE', text_content=value, confidence=1.0))

        if product:
            mapping = {'PRODUCT_NAME': 'product_name', 'BRAND': 'brand', 'GENERIC_NAME': 'generic_name', 'MRP': 'mrp', 'MANUFACTURER_NAME': 'manufacturer', 'MANUFACTURER_ADDRESS': 'address', 'BATCH_NUMBER': None, 'CONSUMER_CARE': 'consumer_care', 'INGREDIENTS_LIST': 'ingredients', 'FSSAI_LICENSE': None}
            attr = mapping.get(field_name)
            if attr:
                setattr(product, attr, value)
        if field_name == 'PRODUCT_NAME':
            inspection.product_name = value
        elif field_name == 'BRAND':
            inspection.brand = value
        
    # Re-evaluate rules
    # 1. Gather all existing extracted fields
    extracted_fields = {f.field_name: {"value": f.field_value, "confidence": f.confidence, "source": f.source} for f in inspection.extracted_fields}
    
    # 2. Add manual input to extracted fields for evaluation
    if input_data.actual_measured_weight is not None:
        val = f"{input_data.actual_measured_weight} {input_data.actual_weight_unit or ''}".strip()
        extracted_fields["ACTUAL_NET_CONTENT"] = {
            "value": val,
            "confidence": 1.0,
            "source": "MANUAL"
        }
    
    # 3. Get rules and applicability
    db_rules = db.query(models.Rule).filter(models.Rule.is_active == True).all()
    all_rules_dict = [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in db_rules]
    
    has_physical = input_data.actual_measured_weight is not None
    applicable_rules = get_applicable_rules(
        all_rules=all_rules_dict,
        category=inspection.category,
        product_type=inspection.product_type or "UNKNOWN",
        package_type=inspection.package_type,
        import_status=inspection.import_status,
        has_physical_data=has_physical
    )
    
    # 4. Evaluate
    rule_results, overall_result = evaluate_rules(applicable_rules, extracted_fields)
    
    # Update inspection
    inspection.overall_result = overall_result
    
    # Update rule results in DB - simple approach: delete old, insert new
    db.query(models.RuleResult).filter(models.RuleResult.inspection_id == inspection.id).delete()
    
    for res in rule_results:
        ev_data = dict(res.get("evidence_data") or {})
        ev_data["binary"] = res.get("binary")
        ev_data["reason"] = res.get("reason") or res.get("message")
        ev_data["validation_result"] = res.get("validation_result")
        ev_data["raw_value"] = res.get("raw_value")
        ev_data["value"] = res.get("value")
        ev_data["unit"] = res.get("unit")
        ev_data["quantity_present"] = res.get("quantity_present")
        ev_data["unit_present"] = res.get("unit_present")
        ev_data["quantity_unit_valid"] = res.get("quantity_unit_valid")

        db.add(models.RuleResult(
            inspection_id=inspection.id,
            rule_id=res.get("rule_id"),
            parameter=res.get("parameter"),
            status=res.get("status"),
            message=res.get("message"),
            evidence_data=ev_data,
            rule_version=res.get("rule_version"),
            regulatory_source=res.get("regulatory_source"),
            rule_reference=res.get("rule_reference"),
            review_required=res.get("review_required")
        ))

    db.commit()
    
    return schemas.MessageResponse(
        message="Manual input saved and rules re-evaluated successfully",
        data={"overall_result": overall_result}
    )


@router.post("/report/{inspection_id}", response_model=schemas.MessageResponse)
def generate_report(inspection_id: int, db: Session = Depends(get_db)):
    """Generate a PDF report for the inspection."""
    inspection = db.query(models.Inspection).filter(models.Inspection.id == inspection_id).first()
    if not inspection:
        raise HTTPException(status_code=404, detail="Inspection not found")
        
    try:
        report = generate_inspection_pdf(inspection, db)
        return schemas.MessageResponse(
            message="Report generated successfully",
            data={"report_id": report.id, "file_url": f"/api/report/{inspection_id}/download"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate report: {str(e)}")


@router.get("/report/{inspection_id}/download")
def download_report(inspection_id: int, db: Session = Depends(get_db)):
    """Download the generated PDF report."""
    inspection = db.query(models.Inspection).filter(models.Inspection.id == inspection_id).first()
    if not inspection or not inspection.report:
        raise HTTPException(status_code=404, detail="Report not found")
        
    file_path = inspection.report.file_path
    import os
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Report file not found on disk")
        
    return FileResponse(
        path=file_path, 
        filename=inspection.report.file_name, 
        media_type="application/pdf"
    )
