//! Test stand-in for SPL Governance `ExecuteTransaction`: invokes the borsh `Vec<InstructionData>`
//! of its instruction data, each signed by the PDA `[b"treasury"]`, the way the governance program
//! signs for its native treasury. The proposal instructions are thereby executed through CPI, with
//! its limits (account growth, signer propagation), not as top-level instructions.

use solana_program::{
    account_info::AccountInfo,
    entrypoint,
    entrypoint::ProgramResult,
    instruction::{AccountMeta, Instruction},
    program::invoke_signed,
    program_error::ProgramError,
    pubkey::Pubkey,
};

entrypoint!(process);

fn take<'a>(data: &'a [u8], offset: &mut usize, len: usize) -> Result<&'a [u8], ProgramError> {
    let end = offset.checked_add(len).ok_or(ProgramError::InvalidInstructionData)?;
    let slice = data.get(*offset..end).ok_or(ProgramError::InvalidInstructionData)?;
    *offset = end;
    Ok(slice)
}

fn take_u32(data: &[u8], offset: &mut usize) -> Result<usize, ProgramError> {
    Ok(u32::from_le_bytes(take(data, offset, 4)?.try_into().unwrap()) as usize)
}

fn take_pubkey(data: &[u8], offset: &mut usize) -> Result<Pubkey, ProgramError> {
    Ok(Pubkey::new_from_array(take(data, offset, 32)?.try_into().unwrap()))
}

fn process(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    let (_, bump) = Pubkey::find_program_address(&[b"treasury"], program_id);
    let mut offset = 0;
    for _ in 0..take_u32(data, &mut offset)? {
        let program = take_pubkey(data, &mut offset)?;
        let mut metas = Vec::new();
        for _ in 0..take_u32(data, &mut offset)? {
            let pubkey = take_pubkey(data, &mut offset)?;
            let flags = take(data, &mut offset, 2)?;
            metas.push(AccountMeta { pubkey, is_signer: flags[0] == 1, is_writable: flags[1] == 1 });
        }
        let len = take_u32(data, &mut offset)?;
        let ix_data = take(data, &mut offset, len)?.to_vec();
        invoke_signed(
            &Instruction { program_id: program, accounts: metas, data: ix_data },
            accounts,
            &[&[b"treasury", &[bump]]],
        )?;
    }
    Ok(())
}
