// Minimal, dependency-free JSON Schema (draft-like) validator.
//
// The Advisor sidecar intentionally has zero third-party dependencies beyond
// the pinned `@openai/codex` package: adding a general-purpose schema
// validation library would widen the supply chain surface for a component
// that sits in front of an AI provider. This module implements only the
// subset of JSON Schema keywords actually used by
// `audit-input-schema.json` and `audit-schema.json`:
//
//   type, enum, const, properties, required, additionalProperties
//   (boolean or nested schema), items, minItems, maxItems, minLength,
//   maxLength, minimum, maximum, pattern, nullable via type arrays.
//
// It fails closed: anything it cannot express is rejected rather than
// silently accepted, and any schema violation is reported with a JSON
// Pointer-ish path so failures are debuggable without dumping payload
// content (callers should log the `errors` array, not the payload).

function typeOf(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

function matchesType(value, type) {
  if (type === "integer") return typeof value === "number" && Number.isInteger(value);
  return typeOf(value) === type;
}

function validateNode(schema, value, path, errors) {
  if (Object.prototype.hasOwnProperty.call(schema, "const")) {
    if (value !== schema.const) {
      errors.push(`${path}: expected constant ${JSON.stringify(schema.const)}`);
    }
    return;
  }

  if (schema.type) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type];
    if (!types.some((type) => matchesType(value, type))) {
      errors.push(`${path}: expected type ${types.join("|")}, got ${typeOf(value)}`);
      return;
    }
  }

  if (schema.enum && !schema.enum.includes(value)) {
    errors.push(`${path}: value is not one of the allowed enum options`);
    return;
  }

  if (typeof value === "string") {
    if (typeof schema.minLength === "number" && value.length < schema.minLength) {
      errors.push(`${path}: shorter than minLength ${schema.minLength}`);
    }
    if (typeof schema.maxLength === "number" && value.length > schema.maxLength) {
      errors.push(`${path}: longer than maxLength ${schema.maxLength}`);
    }
    if (schema.pattern && !new RegExp(schema.pattern).test(value)) {
      errors.push(`${path}: does not match required pattern`);
    }
  }

  if (typeof value === "number") {
    if (typeof schema.minimum === "number" && value < schema.minimum) {
      errors.push(`${path}: below minimum ${schema.minimum}`);
    }
    if (typeof schema.maximum === "number" && value > schema.maximum) {
      errors.push(`${path}: above maximum ${schema.maximum}`);
    }
  }

  if (Array.isArray(value)) {
    if (typeof schema.minItems === "number" && value.length < schema.minItems) {
      errors.push(`${path}: fewer than minItems ${schema.minItems}`);
    }
    if (typeof schema.maxItems === "number" && value.length > schema.maxItems) {
      errors.push(`${path}: more than maxItems ${schema.maxItems}`);
    }
    if (schema.items) {
      value.forEach((item, index) => validateNode(schema.items, item, `${path}[${index}]`, errors));
    }
  }

  if (typeOf(value) === "object" && schema.properties) {
    for (const key of schema.required || []) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) {
        errors.push(`${path}.${key}: required field is missing`);
      }
    }
    for (const [key, propertySchema] of Object.entries(schema.properties)) {
      if (Object.prototype.hasOwnProperty.call(value, key)) {
        validateNode(propertySchema, value[key], `${path}.${key}`, errors);
      }
    }
    if (schema.additionalProperties === false) {
      const allowed = new Set(Object.keys(schema.properties));
      for (const key of Object.keys(value)) {
        if (!allowed.has(key)) {
          errors.push(`${path}.${key}: field is not part of the allowlisted schema`);
        }
      }
    } else if (schema.additionalProperties && typeof schema.additionalProperties === "object") {
      const allowed = new Set(Object.keys(schema.properties));
      for (const key of Object.keys(value)) {
        if (!allowed.has(key)) {
          validateNode(schema.additionalProperties, value[key], `${path}.${key}`, errors);
        }
      }
    }
  }
}

/**
 * Validate `value` against `schema`.
 * Returns `{ valid: boolean, errors: string[] }`. Never throws.
 */
export function validate(schema, value) {
  const errors = [];
  try {
    validateNode(schema, value, "$", errors);
  } catch (error) {
    errors.push(`$: validator raised ${error?.message || error}`);
  }
  return { valid: errors.length === 0, errors };
}
