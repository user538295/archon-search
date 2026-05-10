# Architecture Overview

This system follows a layered architecture with four primary layers: Presentation, Application, Domain, and Infrastructure.

## Presentation Layer
Handles all user-facing concerns: HTTP request parsing, authentication checks, and response serialization. Controllers are thin; they delegate to the Application layer immediately.

## Application Layer
Orchestrates use cases. Each use case class has a single public method that accepts a command object and returns a result. Side effects (email, analytics) are dispatched via the event bus after the use case commits.

## Domain Layer
Contains the business rules. Entities are rich objects—they enforce their own invariants and raise domain exceptions on invalid state transitions. Value objects are immutable and compared by value.

## Infrastructure Layer
Adapters for external systems: database repositories, message brokers, third-party APIs. All adapters implement domain-defined ports (interfaces) so the domain layer has no import dependency on infrastructure.
