'use client'

import { Suspense } from 'react'
import { useSearchParams } from 'next/navigation'
import { SignUp } from '@clerk/nextjs'
import AuthShell from '@/components/AuthShell'
import { clerkAppearance } from '@/lib/clerk-appearance'

/**
 * Someone arriving from an invitation is joining a group that already exists, so
 * sending them through "create a business" is wrong — it asks them to build the
 * thing they were invited to. The invite link carries invited=1 and where they
 * belong in `next`; everyone else still lands on create-business.
 *
 * Only same-origin paths are honoured from `next`. It arrives in a URL, and a URL
 * is not a trustworthy place to read a redirect target from.
 */
function safeNext(raw: string | null): string | null {
  if (!raw) return null
  if (!raw.startsWith('/') || raw.startsWith('//')) return null
  return raw
}

function SignUpInner() {
  const params = useSearchParams()
  const invited = params.get('invited') === '1'
  const next = safeNext(params.get('next'))
  const destination = invited ? next || '/dashboard/stores' : '/dashboard/create-business'

  return (
    <AuthShell headingId="sign-up-heading">
      <h1 id="sign-up-heading" className="sr-only">
        Sign up
      </h1>
      <SignUp
        appearance={clerkAppearance}
        routing="path"
        path="/sign-up"
        signInUrl="/sign-in"
        forceRedirectUrl={destination}
      />
    </AuthShell>
  )
}

export default function SignUpPage() {
  // useSearchParams needs a Suspense boundary to keep this route statically
  // renderable.
  return (
    <Suspense fallback={<AuthShell headingId="sign-up-heading"><div /></AuthShell>}>
      <SignUpInner />
    </Suspense>
  )
}
